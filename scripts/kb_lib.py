from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import shutil
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kb_kinds import DEFAULT_KIND, VALID_KINDS


class JsonlSafetyError(RuntimeError):
    """Raised when a JSONL file cannot be read safely."""


class PersonalKbConfigError(ValueError):
    """Raised when the canonical Personal KB configuration is invalid."""


@dataclass(frozen=True)
class StorageLayout:
    """Canonical single-root layout derived from config.json.

    ``records`` is the only durable retrieval source. The other directories
    contain evidence, manifests, runtime events, or rebuildable caches and
    are always resolved below the same root.
    """

    root: Path
    records: Path
    retained_files: Path
    manifests: Path
    runtime: Path
    cache: Path


_GIT_CONFLICT_MARKER_RE = re.compile(
    r"^(?:<<<<<<<(?: .*)?|=======|>>>>>>>(?: .*)?)\s*$",
    re.MULTILINE,
)

# 尝试导入时间索引模块（可选依赖）
try:
    import importlib.util

    _kb_backend_path = Path(__file__).parent.parent / "backend" / "time_index.py"
    if _kb_backend_path.exists():
        spec = importlib.util.spec_from_file_location("time_index", _kb_backend_path)
        if spec and spec.loader:
            _time_index_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_time_index_module)
            _get_time_index = _time_index_module.get_time_index
            _TIME_INDEX_AVAILABLE = True
        else:
            _TIME_INDEX_AVAILABLE = False
            _get_time_index = None
    else:
        _TIME_INDEX_AVAILABLE = False
        _get_time_index = None
except Exception:
    _TIME_INDEX_AVAILABLE = False
    _get_time_index = None


def _update_time_index_for_entry(entry_id: str) -> None:
    """更新时间索引中的指定条目（写入后钩子）"""
    if not _TIME_INDEX_AVAILABLE or not _get_time_index:
        return
    try:
        kb_root = kb_base_dir().parent
        index = _get_time_index(kb_root=str(kb_root))
        index.update_entry(entry_id)
    except Exception:
        pass


@dataclass(frozen=True)
class RepoContext:
    repo_name: str
    branch: str
    branch_dir: str
    repo_dir: Path
    branch_path: Path
    kb_path: Path
    summary_path: Path
    index_path: Path
    archive_dir: Path
    attachments_dir: Path
    workspace_dir: str  # 强制记录当前工作目录的绝对路径
    routing_source: str = "unknown"
    candidate_repos: tuple[str, ...] = ()


WINDOWS_RESERVED_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def now_iso() -> str:
    """返回 ISO 格式时间戳（毫秒精度），用于生成唯一 ID 和时间排序"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def json_object_from_b64(value: str, label: str = "base64 JSON") -> dict[str, Any]:
    """从 base64 编码的 UTF-8 JSON 字符串解码为 dict

    Args:
        value: base64 编码的 JSON 字符串
        label: 用于错误消息的参数名称

    Returns:
        解码后的 JSON 对象（dict）

    Raises:
        ValueError: 如果 base64 解码失败、JSON 解析失败或结果不是 dict
    """
    try:
        raw = base64.b64decode(value, validate=True)
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Invalid {label}: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must decode to a JSON object")
    return parsed


def find_entry(entries: list[dict[str, Any]], entry_id: str) -> int | None:
    """在条目列表中查找指定 ID 的条目索引

    先按 'id' 字段精确匹配，如果找不到则尝试根据 ts+title 计算 ID 匹配

    Args:
        entries: 条目列表
        entry_id: 要查找的条目 ID（8字符十六进制）

    Returns:
        条目在列表中的索引，如果未找到返回 None
    """
    # 先尝试精确匹配 id 字段
    for i, e in enumerate(entries):
        if e.get("id") == entry_id:
            return i
    # 尝试计算 ID 匹配
    for i, e in enumerate(entries):
        computed = generate_entry_id(e.get("ts", ""), e.get("title", ""))
        if computed == entry_id:
            return i
    return None


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _repo_root(cwd: Path) -> Path | None:
    code, out, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if code != 0 or not out:
        return None
    return Path(out)


def _current_branch(cwd: Path) -> str | None:
    code, out, _ = _run_git(["branch", "--show-current"], cwd=cwd)
    if code == 0 and out:
        return out

    # Detached HEAD or git not ready; fallback to short sha.
    code2, out2, _ = _run_git(["rev-parse", "--short", "HEAD"], cwd=cwd)
    if code2 == 0 and out2:
        return f"detached-{out2}"
    return None


def _find_child_git_repo(cwd: Path) -> Path | None:
    """向下查找一级子目录中的 git 仓库。

    如果发现多个子 git 仓库，输出警告到 stderr 并返回 None（不再盲目选择第一个）。
    如果只有一个子 git 仓库，返回该仓库根目录。
    如果没有子 git 仓库，返回 None。
    """
    if not cwd.is_dir():
        return None
    try:
        child_repos = []
        for child in cwd.iterdir():
            if not child.is_dir():
                continue
            git_dir = child / ".git"
            if git_dir.exists():
                child_repos.append(child)

        if len(child_repos) == 0:
            return None
        elif len(child_repos) == 1:
            return child_repos[0]
        else:
            # 发现多个子 git 仓库，输出警告但不中断（防御性处理）
            import sys
            repo_names = [r.name for r in child_repos]
            sys.stderr.write(
                f"⚠️  当前目录下发现 {len(child_repos)} 个 git 子仓库：{', '.join(repo_names)}\n"
                f"   无法自动判断应写入哪个桶，请先 cd 到具体项目目录，或使用 --repo 参数指定。\n"
                f"   当前 KB 将写入 no-repo/no-branch 桶（通常不是你想要的）。\n"
            )
            return None
    except (OSError, PermissionError):
        pass
    return None


def _find_all_child_git_repos(cwd: Path) -> list[Path]:
    """向下查找一级子目录中的所有 git 仓库，返回列表。"""
    if not cwd.is_dir():
        return []
    try:
        child_repos = []
        for child in cwd.iterdir():
            if not child.is_dir():
                continue
            git_dir = child / ".git"
            if git_dir.exists():
                child_repos.append(child)
        return child_repos
    except (OSError, PermissionError):
        return []


def _score_repo_by_keywords(task_keywords: list[str], repo_name: str, config: dict) -> float:
    """根据任务关键词计算仓库匹配分数。"""
    project_keywords = config.get("smart_routing", {}).get("project_keywords", {})
    repo_config = project_keywords.get(repo_name, {})
    repo_keywords = repo_config.get("keywords", [])
    weight = repo_config.get("weight", 1.0)

    if not repo_keywords:
        return 0.0

    match_count = 0
    for task_kw in task_keywords:
        task_kw_lower = task_kw.lower()
        for repo_kw in repo_keywords:
            if repo_kw.lower() in task_kw_lower or task_kw_lower in repo_kw.lower():
                match_count += 1
                break  # 每个任务关键词最多匹配一次

    return match_count * weight


def _extract_task_keywords(task_hint: str) -> list[str]:
    """从任务提示中提取关键词（简单分词）。"""
    if not task_hint:
        return []

    # 简单分词：按空格、标点分隔
    import re
    words = re.split(r'[\s,，、。；;：:]+', task_hint)
    # 过滤短词（<2 字符）和纯数字
    keywords = [w.strip() for w in words if len(w.strip()) >= 2 and not w.strip().isdigit()]
    return keywords[:10]  # 最多保留 10 个关键词


def _infer_repo_from_task_hint(
    child_repos: list[Path],
    task_hint: str | None,
    config: dict,
    *,
    debug: bool = False,
) -> Path | None:
    """从任务提示推断正确的仓库（直接读倒排索引，不依赖 config.json）。"""
    if not task_hint:
        return None

    smart_routing = config.get("smart_routing", {})
    if not smart_routing.get("enabled", True):
        return None

    task_keywords = _extract_task_keywords(task_hint)
    if not task_keywords:
        return None

    # 优先：直接从倒排索引读取映射
    try:
        import importlib.util
        _kb_ii_path = Path(__file__).parent.parent / "backend" / "inverted_index.py"
        if _kb_ii_path.exists():
            spec = importlib.util.spec_from_file_location("inverted_index", _kb_ii_path)
            if spec and spec.loader:
                ii_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(ii_module)

                index_path = ii_module.get_inverted_index_path(kb_base_dir())
                index = ii_module.InvertedIndex(index_path)
                index.load()

                # 统计每个 repo 的匹配分数
                repo_scores = {}
                for kw in task_keywords:
                    kw_info = index.get_keyword_info(kw)
                    if not kw_info:
                        continue

                    buckets = kw_info.get("buckets", [])
                    for bucket in buckets:
                        parts = bucket.split("/")
                        if not parts:
                            continue

                        # 跳过特殊桶
                        if parts[0] in ("_global", "no-repo", "test-repo"):
                            continue

                        # 检查桶路径的所有层级是否匹配任意子仓库名
                        # 桶路径格式：parent/child_repo/branch 或 repo/branch
                        for r in child_repos:
                            if r.name in parts:
                                repo_scores[r.name] = repo_scores.get(r.name, 0) + 1
                                break

                if repo_scores:
                    # 返回得分最高的 repo
                    best_repo_name = max(repo_scores, key=repo_scores.get)
                    best_score = repo_scores[best_repo_name]

                    confidence_threshold = smart_routing.get("confidence_threshold", 2)
                    if best_score >= confidence_threshold:
                        for repo in child_repos:
                            if repo.name == best_repo_name:
                                if debug:
                                    sys.stderr.write(
                                        f"✓ 从倒排索引推断仓库：{best_repo_name}（匹配分数={best_score}, "
                                        f"任务关键词={task_keywords[:3]}）\n"
                                    )
                                return repo
    except Exception:
        pass

    # 回退：尝试从 config.json 读取（兼容旧方式）
    scores = []
    for repo in child_repos:
        score = _score_repo_by_keywords(task_keywords, repo.name, config)
        if score > 0:
            scores.append((repo, score))

    if not scores:
        return None

    # 排序并选择最高分
    scores.sort(key=lambda x: x[1], reverse=True)
    best_repo, best_score = scores[0]

    # 检查置信度阈值
    confidence_threshold = smart_routing.get("confidence_threshold", 2)
    if best_score >= confidence_threshold:
        if debug:
            sys.stderr.write(
                f"✓ 推断仓库：{best_repo.name}（匹配分数={best_score}, "
                f"任务关键词={task_keywords[:3]}, 候选仓库={len(child_repos)}）\n"
            )
        return best_repo

    return None


def _safe_dir_name(value: str, *, fallback: str, allow_slash: bool = False) -> str:
    """
    将字符串转换为安全的目录名。

    参数:
        value: 原始字符串
        fallback: 当 value 为空或无效时使用的默认值
        allow_slash: 是否允许保留路径分隔符（用于支持多级嵌套）
    """
    if not value:
        return fallback

    # 如果允许保留路径分隔符，统一转换为 /
    if allow_slash:
        safe = value.replace("\\", "/")
    else:
        # 原有逻辑：将路径分隔符替换为 __
        safe = value.replace("/", "__").replace("\\", "__")

    # Replace characters invalid on Windows filesystems.
    safe = re.sub(r'[<>:"|?*]', "_", safe)
    safe = re.sub(r"[\x00-\x1f]", "_", safe)

    safe = safe.strip().rstrip(". ")
    if not safe:
        safe = fallback

    # 保留路径时，检查每个片段是否为保留名
    if allow_slash and "/" in safe:
        segments = safe.split("/")
        for i, seg in enumerate(segments):
            if seg.lower() in WINDOWS_RESERVED_DEVICE_NAMES:
                segments[i] = f"{fallback}-{seg}"
        safe = "/".join(segments)
    elif safe.lower() in WINDOWS_RESERVED_DEVICE_NAMES:
        safe = f"{fallback}-{safe}"

    return safe


def _userprofile_dir() -> Path:
    env = os.environ.get("USERPROFILE")
    return Path(env) if env else Path.home()


def skill_root_dir() -> Path:
    """返回当前 personal-kb skill 根目录。"""
    return Path(__file__).resolve().parent.parent


def _normalize_personal_kb_root(value: str | Path) -> Path:
    """把环境变量/配置中的路径统一成 personal-kb 根目录。

    支持两种输入：
    - `/path/to/personal-kb`
    - `/path/to/personal-kb/repos`
    """
    raw = os.path.expandvars(str(value).strip())
    if not raw or "${" in raw:
        raise PersonalKbConfigError("Personal KB root 不能为空，且必须先解析环境变量")
    root = Path(raw).expanduser()
    if root.name == "repos":
        root = root.parent
    # Do not silently create a relative/ambiguous second data source.
    if not root.is_absolute():
        raise PersonalKbConfigError(f"Personal KB root 必须是绝对路径: {value}")
    return root.resolve(strict=False)


def _configured_root_from_env() -> Path | None:
    for key in ("PERSONAL_KB_ROOT", "PERSONAL_KB_HOME"):
        value = os.environ.get(key)
        if value and value.strip():
            return _normalize_personal_kb_root(value)
    return None


def personal_kb_root_dir() -> Path:
    """返回 personal-kb 数据根目录。

    查找顺序：
    1. 明确设置的环境变量 `PERSONAL_KB_ROOT` / `PERSONAL_KB_HOME`
    2. 当前 Skill 配置中的 `storage.root`

    不再回退到工作目录或历史 Windows 路径，避免出现无法察觉的影子 KB。
    """
    env_root = _configured_root_from_env()
    if env_root is not None:
        return env_root

    config = load_config()
    storage = config.get("storage") if isinstance(config, dict) else None
    storage_root = storage.get("root") if isinstance(storage, dict) else None
    if not isinstance(storage_root, str) or not storage_root.strip():
        raise PersonalKbConfigError(
            "未配置 Personal KB 数据根。请在 config.json 设置 storage.root，"
            "或显式设置 PERSONAL_KB_ROOT（仅建议用于测试）。"
        )
    return _normalize_personal_kb_root(storage_root)


def _resolve_storage_child(root: Path, value: Any, key: str, default: str) -> Path:
    raw = default if value is None else str(value).strip()
    if not raw:
        raw = default
    raw = os.path.expandvars(raw)
    child = Path(raw).expanduser()
    if child.is_absolute():
        raise PersonalKbConfigError(f"storage.{key} 必须是 root 下的相对路径: {raw}")
    if any(part == ".." for part in child.parts):
        raise PersonalKbConfigError(f"storage.{key} 不得包含 ..: {raw}")
    resolved_root = root.resolve(strict=False)
    resolved = (resolved_root / child).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PersonalKbConfigError(f"storage.{key} 逃出了 canonical root: {raw}") from exc
    return resolved


def storage_layout() -> StorageLayout:
    """Return all configured paths under the single canonical root."""
    root = personal_kb_root_dir()
    config = load_config()
    storage = config.get("storage") if isinstance(config, dict) else {}
    if not isinstance(storage, dict):
        storage = {}
    return StorageLayout(
        root=root,
        records=_resolve_storage_child(root, storage.get("records"), "records", "repos"),
        retained_files=_resolve_storage_child(root, storage.get("retained_files"), "retained_files", "retained-files"),
        manifests=_resolve_storage_child(root, storage.get("manifests"), "manifests", "manifests"),
        runtime=_resolve_storage_child(root, storage.get("runtime"), "runtime", "runtime"),
        cache=_resolve_storage_child(root, storage.get("cache"), "cache", "cache"),
    )


def kb_base_dir() -> Path:
    return storage_layout().records


def retained_files_dir() -> Path:
    return storage_layout().retained_files


def manifests_dir() -> Path:
    return storage_layout().manifests


def runtime_dir() -> Path:
    return storage_layout().runtime


def cache_dir() -> Path:
    return storage_layout().cache


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return str(left) == str(right)


def runtime_dir_for_records(records_dir: Path | None = None) -> Path:
    """Resolve runtime storage for canonical records or an explicit test corpus."""
    if records_dir is None or _same_resolved_path(Path(records_dir), kb_base_dir()):
        return runtime_dir()
    return Path(records_dir) / "_meta"


def cache_dir_for_records(records_dir: Path | None = None) -> Path:
    """Resolve derived-cache storage for canonical records or an explicit test corpus."""
    if records_dir is None or _same_resolved_path(Path(records_dir), kb_base_dir()):
        return cache_dir()
    return Path(records_dir).parent / "_meta"


def runtime_file(name: str, *, base_dir: Path | None = None) -> Path:
    """Resolve a runtime file while retaining explicit test-base compatibility."""
    return runtime_dir_for_records(base_dir) / name


def global_bucket_dir() -> Path:
    """全局桶路径，用于存放跨项目通用的术语、约定等。"""
    return kb_base_dir() / "_global" / "_shared"


def generate_entry_id(ts: str, title: str) -> str:
    """基于时间戳+标题生成短 ID（8 位 hex），用于条目定位。"""
    raw = f"{ts}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def load_synonyms() -> dict[str, list[str]]:
    """加载同义词表（references/synonyms.json）。"""
    syn_path = Path(__file__).parent.parent / "references" / "synonyms.json"
    if not syn_path.exists():
        return {}
    try:
        with syn_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(v, list) and not k.startswith("_")}
    except (json.JSONDecodeError, OSError):
        return {}


def expand_query(query: str, synonyms: dict[str, list[str]] | None = None) -> list[str]:
    """将查询词展开为包含同义词的列表（支持双向展开）。"""
    if synonyms is None:
        synonyms = load_synonyms()
    q = query.strip().lower()
    if not q:
        return []
    terms = [q]

    # 1. 正向展开：查询词是 key
    if q in synonyms:
        for syn in synonyms[q]:
            if syn.lower() not in terms:
                terms.append(syn.lower())

    # 2. 反向展开：查询词在某个 value 列表中
    for key, values in synonyms.items():
        if any(q == v.lower() for v in values):
            # 找到包含查询词的同义词组，将 key + 其他 value 都加入
            if key.lower() not in terms:
                terms.append(key.lower())
            for syn in values:
                if syn.lower() not in terms:
                    terms.append(syn.lower())
            break  # 只匹配第一个同义词组（避免重复遍历）

    return terms


def resolve_context(
    cwd: str | Path | None = None,
    *,
    repo_name_override: str | None = None,
    branch_override: str | None = None,
    task_hint: str | None = None,
    operation: str = "write",
    debug: bool = False,
) -> RepoContext:
    """解析 KB 存储上下文（仓库、分支、桶路径）。

    Args:
        cwd: 当前工作目录
        repo_name_override: 用户明确指定的仓库名
        branch_override: 用户明确指定的分支名
        task_hint: 任务描述或关键词，用于智能推断仓库（多子仓库场景）
        operation: 当前用途，"search" 时提示只读搜索归属，其他值按写入归属提示
        debug: 是否输出内部路由诊断；默认成功路径静默

    Returns:
        RepoContext: 包含仓库名、分支名、桶路径等信息
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    workspace_dir = str(cwd_path.resolve())  # 强制记录绝对路径
    config = load_config()

    # 1. 先检查当前目录是否有 git 仓库
    repo_root = None if repo_name_override and branch_override else _repo_root(cwd_path)
    child_repo = None
    routing_source = "explicit_override" if repo_name_override else ("current_repo" if repo_root else "cwd_fallback")
    candidate_repos: tuple[str, ...] = ()

    # 2. 如果当前目录没有，向下查找子目录中的 git 仓库
    if not repo_name_override and not repo_root:
        child_repos = _find_all_child_git_repos(cwd_path)
        candidate_repos = tuple(repo.name for repo in child_repos)
        if len(child_repos) == 1:
            # 只有一个子仓库，直接使用
            child_repo = child_repos[0]
            repo_root = _repo_root(child_repo)
            routing_source = "single_child"
        elif len(child_repos) > 1:
            # 多个子仓库，尝试智能推断
            inferred_repo = _infer_repo_from_task_hint(child_repos, task_hint, config, debug=debug)
            if inferred_repo:
                child_repo = inferred_repo
                repo_root = _repo_root(child_repo)
                routing_source = "task_hint"
            else:
                # 推断失败——写入父目录桶（cwd_name/no-git/kb.jsonl）
                # 代表"和这个目录下的项目相关，但不确定是哪个"
                # 越用越精确：后续 AI 能判断归属时，用 kb_migrate 迁移到具体子仓库桶
                routing_source = "workspace_fallback"
                if debug:
                    action = "搜索" if operation == "search" else "写入"
                    sys.stderr.write(
                        f"多仓库未确定归属，{action}父目录桶 {cwd_path.name}/\n"
                        f"候选子仓库：{', '.join(candidate_repos)}\n"
                    )
                # child_repo 保持 None，后续走 else 分支 → 写入 cwd_name/no-git 桶

    # 3. 构建层级桶路径
    # 如果有子git仓库，桶路径 = parent/child/child_branch
    # 否则，桶路径 = repo/branch
    if child_repo and repo_root:
        # 父目录信息
        parent_name = repo_name_override or cwd_path.name
        # 如果包含路径分隔符，保留嵌套结构
        if "/" in parent_name or "\\" in parent_name:
            parent_name = _safe_dir_name(parent_name, fallback="unknown-repo", allow_slash=True).lower()
        else:
            parent_name = _safe_dir_name(parent_name, fallback="unknown-repo").lower()

        # 子仓库信息
        child_name = repo_root.name
        child_name = _safe_dir_name(child_name, fallback="unknown-repo").lower()
        child_branch = branch_override or _current_branch(repo_root) or "no-git"
        child_branch_dir = _safe_dir_name(child_branch, fallback="unknown-branch")

        # 层级路径: parent/child/child_branch（去掉冗余的 parent_branch）
        # 如果 parent_name 包含 /，则已经是嵌套路径
        if "/" in parent_name:
            repo_name = f"{parent_name}/{child_name}"
            # repo_dir 按照 parent_name 的路径结构构建
            repo_dir = kb_base_dir()
            for part in parent_name.split("/"):
                repo_dir = repo_dir / part
            repo_dir = repo_dir / child_name
        else:
            repo_name = f"{parent_name}/{child_name}"
            repo_dir = kb_base_dir() / parent_name / child_name

        branch = child_branch
        branch_dir = child_branch_dir
        branch_path = repo_dir / branch_dir
    else:
        # 当前目录就是git仓库，或者没有任何git
        repo_name = repo_name_override
        if not repo_name:
            if repo_root:
                repo_name = repo_root.name
            else:
                repo_name = cwd_path.name

        # 如果 repo_name 包含路径分隔符（如 example-project-b/no-git/example-branch-b），保留嵌套结构
        # 判断路径是否已经包含分支信息（最后一个segment是分支名）
        if "/" in repo_name or "\\" in repo_name:
            repo_name_safe = _safe_dir_name(repo_name, fallback="unknown-repo", allow_slash=True).lower()
            parts = repo_name_safe.split("/")

            # 检查是否已经包含分支信息：
            # 如果倒数第二个是 "no-git" 或者最后有3+个部分，认为已包含分支
            # 例如：example-project-b/no-git/example-branch-b 中，no-git 是父目录分支，example-branch-b 是子仓库
            # 正确结构应该是：example-project-b/no-git/example-branch-b/master（还需要子仓库分支）

            repo_name = repo_name_safe
            repo_dir = kb_base_dir()
            for part in parts:
                repo_dir = repo_dir / part

            branch = branch_override
            if not branch:
                if repo_root:
                    branch = _current_branch(repo_root)
            if not branch:
                branch = "no-git"

            branch_dir = _safe_dir_name(branch, fallback="unknown-branch")
            branch_path = repo_dir / branch_dir
        else:
            repo_name = _safe_dir_name(repo_name, fallback="unknown-repo").lower()
            repo_dir = kb_base_dir() / repo_name

            branch = branch_override
            if not branch:
                if repo_root:
                    branch = _current_branch(repo_root)
            if not branch:
                branch = "no-git"

            branch_dir = _safe_dir_name(branch, fallback="unknown-branch")
            branch_path = repo_dir / branch_dir

    return RepoContext(
        repo_name=repo_name,
        branch=branch,
        branch_dir=branch_dir,
        repo_dir=repo_dir,
        branch_path=branch_path,
        kb_path=branch_path / "kb.jsonl",
        summary_path=branch_path / "summary.jsonl",
        index_path=branch_path / "index.json",
        archive_dir=branch_path / "archive",
        attachments_dir=branch_path / "attachments",
        workspace_dir=workspace_dir,
        routing_source=routing_source,
        candidate_repos=candidate_repos,
    )


def ensure_branch_layout(ctx: RepoContext) -> None:
    ctx.branch_path.mkdir(parents=True, exist_ok=True)
    ctx.archive_dir.mkdir(parents=True, exist_ok=True)
    ctx.attachments_dir.mkdir(parents=True, exist_ok=True)


def _git_path_is_unmerged(path: Path) -> bool:
    """Return whether Git's index contains unresolved stages for ``path``."""
    repo_root = _repo_root(path.parent)
    if repo_root is None:
        return False
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    code, out, _ = _run_git(["ls-files", "-u", "--", relative.as_posix()], cwd=repo_root)
    return code == 0 and bool(out)


def ensure_jsonl_safe(path: Path, *, content: str | None = None) -> None:
    """Fail closed when the target JSONL has an unresolved Git conflict.

    Invalid legacy rows remain a compatibility concern and are still skipped by
    ``read_jsonl``. Git conflict markers and unmerged index stages are different:
    silently skipping those rows could combine both sides into an invented KB
    view, so every reader must stop instead.
    """
    if not path.exists():
        return
    if content is None:
        content = path.read_text(encoding="utf-8", errors="replace")
    if _GIT_CONFLICT_MARKER_RE.search(content) or _git_path_is_unmerged(path):
        raise JsonlSafetyError(
            f"Unresolved Git conflict in KB JSONL: {path}. Resolve the conflict before reading or writing this bucket."
        )


def _bucket_lock_path(path: Path) -> Path:
    """Return a stable machine-local lock path without dirtying the KB repo."""
    lock_root = Path(tempfile.gettempdir()) / "personal-kb-locks"
    key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return lock_root / f"{key}.lock"


@contextmanager
def bucket_lock(path: Path):
    """Serialize one bucket's complete read-modify-write transaction."""
    lock_path = _bucket_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if sys.platform == "win32" and lock_path.stat().st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        _acquire_file_lock(lock_file)
        try:
            yield
        finally:
            _release_file_lock(lock_file)


def append_jsonl(path: Path, obj: dict[str, Any], *, lock_held: bool = False) -> None:
    """原子性追加一条 JSONL 记录，使用 O_APPEND 保证并发安全"""
    lock_context = nullcontext() if lock_held else bucket_lock(path)
    with lock_context:
        path.parent.mkdir(parents=True, exist_ok=True)
        ensure_jsonl_safe(path)
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        line_bytes = (line + "\n").encode("utf-8")

        # O_APPEND 保证单行写入原子性；固定 bucket lock 与重写事务协调。
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line_bytes)
        finally:
            os.close(fd)

    # 写入后更新时间索引（如果条目有 ID）
    entry_id = obj.get("id")
    if entry_id:
        _update_time_index_for_entry(entry_id)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not path.exists():
        return items
    content = path.read_text(encoding="utf-8", errors="replace")
    ensure_jsonl_safe(path, content=content)
    for raw in content.splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


def _count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            if raw.strip():
                n += 1
    return n


# 混合型 kind：既有通用经验又有项目特定细节，需要分层存储
MIXED_TYPES = {
    "map", "issue", "pitfall", "requirement", "implementation"
}

# 纯项目特定 kind：本身就是描述当前项目的差异，跨项目搜索时需要标注来源
PURE_PROJECT_TYPES = {
    "requirement", "implementation"
}

# 纯通用 kind：不含项目特定细节，跨项目搜索时直接返回
PURE_GENERIC_TYPES = {
    "experience"
}

# 核心检索 kind：必须能被中文别名和证据路径稳定命中。
CORE_REQUIRED_TYPES = {
    "map", "issue", "pitfall", "requirement", "implementation",
}


QUALITY_REQUIRED_TYPES = MIXED_TYPES | CORE_REQUIRED_TYPES


def _non_empty_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [item for item in value if str(item).strip()]


def validate_entry_fields(entry: dict[str, Any]) -> tuple[bool, str]:
    """验证条目字段完整性。

    P0 只强制最低写入质量，避免为了记录可复用结论而被复杂分层挡住。
    aliases、source_paths/key_files、confidence、transferable、project_specific
    仍然是推荐字段；如果提供，就检查基本类型和值域。

    返回：(是否通过, 错误信息)
    """
    if "type" in entry:
        return False, "type 字段已废弃，新记录必须使用 kind"

    entry_kind = entry.get("kind", DEFAULT_KIND)
    if entry_kind not in VALID_KINDS:
        return False, f"{entry_kind} 不是有效 kind，有效值: {', '.join(sorted(VALID_KINDS))}"

    title = entry.get("title")
    story = entry.get("story")
    if not (isinstance(title, str) and title.strip()) and not (isinstance(story, str) and story.strip()):
        return False, f"{entry_kind} 类型必须提供 title 或 story"

    aliases = entry.get("aliases")
    if aliases is not None and not isinstance(aliases, list):
        return False, f"{entry_kind} 类型的 aliases 必须是列表"

    key_files = entry.get("key_files")
    if key_files is not None and not isinstance(key_files, list):
        return False, f"{entry_kind} 类型的 key_files 必须是列表"

    source_paths = entry.get("source_paths")
    if source_paths is not None and not isinstance(source_paths, list):
        return False, f"{entry_kind} 类型的 source_paths 必须是列表"

    confidence = entry.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return False, f"{entry_kind} 类型的 confidence 必须是 0-1 数值"
        if confidence < 0 or confidence > 1:
            return False, f"{entry_kind} 类型的 confidence 必须在 0-1 范围内"

    transferable = entry.get("transferable")
    if transferable is not None and not isinstance(transferable, dict):
        return False, f"{entry_kind} 类型的 transferable 必须是对象"

    project_specific = entry.get("project_specific")
    if project_specific is not None and not isinstance(project_specific, dict):
        return False, f"{entry_kind} 类型的 project_specific 必须是对象"

    return True, ""


def _acquire_file_lock(f):
    """跨平台文件锁（排他锁）"""
    if sys.platform == "win32":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _release_file_lock(f):
    """释放文件锁"""
    if sys.platform == "win32":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _rewrite_jsonl(path: Path, entries: list[dict[str, Any]], *, lock_held: bool = False) -> None:
    """原子性重写 JSONL 文件，使用固定 bucket 锁、临时文件和重命名。"""
    lock_context = nullcontext() if lock_held else bucket_lock(path)
    with lock_context:
        _rewrite_jsonl_unlocked(path, entries)


def _rewrite_jsonl_unlocked(path: Path, entries: list[dict[str, Any]]) -> None:
    """Rewrite while the caller owns the stable bucket lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_jsonl_safe(path)

    # 1. 备份原文件（保留最近 3 个备份）
    if path.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_suffix(f"{path.suffix}.bak-{timestamp}")
        shutil.copy2(path, backup_path)

        # 清理旧备份（保留最近 3 个）
        backup_pattern = f"{path.name}.bak-*"
        backups = sorted(path.parent.glob(backup_pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_backup in backups[3:]:
            try:
                old_backup.unlink()
            except:
                pass

    # 2. 写入临时文件（带文件锁保护）
    temp_fd, temp_path_str = tempfile.mkstemp(
        dir=path.parent,
        prefix=".tmp_",
        suffix=".jsonl"
    )
    temp_path = Path(temp_path_str)

    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as f:
            _acquire_file_lock(f)
            try:
                for obj in entries:
                    f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
                    f.write("\n")
            finally:
                _release_file_lock(f)

        # 3. 原子替换目标文件。os.replace 在 Windows 上会直接替换已有文件，
        # 避免先 unlink 造成短暂丢文件窗口。
        os.replace(str(temp_path), str(path))
    except:
        # 清理临时文件
        if temp_path.exists():
            temp_path.unlink()
        raise


def compute_index(ctx: RepoContext) -> dict[str, Any]:
    kb_bytes = ctx.kb_path.stat().st_size if ctx.kb_path.exists() else 0
    summary_bytes = ctx.summary_path.stat().st_size if ctx.summary_path.exists() else 0

    return {
        "updated_ts": now_iso(),
        "repo": ctx.repo_name,
        "branch": ctx.branch,
        "branch_dir": ctx.branch_dir,
        "paths": {
            "kb": str(ctx.kb_path),
            "summary": str(ctx.summary_path),
            "archive_dir": str(ctx.archive_dir),
            "attachments_dir": str(ctx.attachments_dir),
        },
        "kb": {"bytes": kb_bytes, "entries": _count_jsonl_lines(ctx.kb_path)},
        "summary": {"bytes": summary_bytes, "entries": _count_jsonl_lines(ctx.summary_path)},
    }


def write_index(ctx: RepoContext) -> None:
    ctx.branch_path.mkdir(parents=True, exist_ok=True)
    index = compute_index(ctx)
    with ctx.index_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ==================== 新增：配置加载 ====================

def load_config() -> dict[str, Any]:
    """加载唯一配置源。

    `PERSONAL_KB_CONFIG` 是明确的配置选择；缺失或损坏时直接报错。
    Skill 自带的 `config.json` 仅用于本地开发，公开发布包只提供
    `config.example.json`，不会隐式读取其他机器上的历史路径。
    """
    env_config = os.environ.get("PERSONAL_KB_CONFIG")
    config_path = Path(env_config).expanduser() if env_config else skill_root_dir() / "config.json"
    if not config_path.exists():
        if env_config:
            raise PersonalKbConfigError(f"PERSONAL_KB_CONFIG 指向的文件不存在: {config_path}")
        # 允许导入模块和单元测试使用显式 PERSONAL_KB_ROOT；真正访问数据时
        # personal_kb_root_dir() 会给出缺失 storage.root 的可操作错误。
        return {
            "storage": {},
            "runtime": {"mode": "normal"},
            "challenge": {
                "success_sample_rate": 0.10,
                "max_adopted_entries": 3,
                "critique_depth": 1,
                "defer_success_samples": True,
            },
        }
    try:
        with config_path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise PersonalKbConfigError(f"无法读取 Personal KB 配置 {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PersonalKbConfigError(f"Personal KB 配置必须是 JSON 对象: {config_path}")
    return data


def default_config() -> dict[str, Any]:
    """Return a portable config template for new installations."""
    return {
        "storage": {
            "root": "${PERSONAL_KB_ROOT}",
            "records": "repos",
            "retained_files": "retained-files",
            "manifests": "manifests",
            "runtime": "runtime",
            "cache": "cache",
        },
        "runtime": {"mode": "normal"},
        "challenge": {
            "success_sample_rate": 0.10,
            "max_adopted_entries": 3,
            "critique_depth": 1,
            "defer_success_samples": True,
        },
        "cleanup": {
            "auto_merge_mode": "ai",
            "exact_duplicate_threshold": 0.95,
            "same_issue_strategy": {
                "mode": "ai",
                "same_day_merge": True,
                "time_span_hours": 2,
                "title_similarity": 0.85,
                "complementary_types": True
            },
            "fix_superseded_state": True,
            "log_level": "info"
        },
        "search": {
            "expand_queries": True,
            "expand_ip_addresses": True,
            "expand_abbreviations": True,
            "include_archived_on_empty": True
        },
        "write": {
            "default_update_mode": "auto",
            "promotion_threshold": 10,
            "demotion_days": 30
        }
    }


# ==================== 新增：智能判断函数 ====================

def should_replace_old_entry(old_entry: dict[str, Any], new_context: str) -> tuple[bool, str]:
    """判断是替换旧条目还是保留为里程碑。

    Args:
        old_entry: 旧条目字典
        new_context: 新内容的上下文描述

    Returns:
        (是否替换, 原因)
    """
    # 小改动关键词
    MINOR_UPDATE_PATTERNS = [
        "参数调整", "配置微调", "修正错误", "补充说明",
        "小改动", "优化", "完善", "更新"
    ]

    # 方向变化关键词
    MAJOR_CHANGE_KEYWORDS = [
        "推翻", "废弃", "改用", "不再", "全部改为",
        "重新设计", "架构调整", "技术栈切换", "迁移"
    ]

    # 检测关键词
    context_lower = new_context.lower()

    if any(kw in context_lower for kw in MINOR_UPDATE_PATTERNS):
        return True, "小改动，直接替换"

    if any(kw in context_lower for kw in MAJOR_CHANGE_KEYWORDS):
        return False, "方向变化，保留里程碑"

    # 默认：替换
    return True, "无明确理由，默认替换"


# ==================== 新增：查询展开函数 ====================

def expand_query_variants(query: str) -> list[str]:
    """智能查询展开：生成多个查询变体。

    Args:
        query: 原始查询字符串

    Returns:
        查询变体列表（包含原始查询）
    """
    variants = [query]
    query_lower = query.lower()

    # 1. 中英互译
    translations = {
        "服务器": "server",
        "路径": "path",
        "配置": "config",
        "部署": "deploy",
        "数据库": "database",
        "错误": "error",
        "日志": "log"
    }

    for zh, en in translations.items():
        if zh in query:
            variants.append(query.replace(zh, en))
        if en in query_lower:
            variants.append(query.replace(en, zh))

    # 2. IP 地址展开
    if "143" in query and "192.168" not in query:
        variants.append(query.replace("143", "192.168.2.143"))
        variants.append(query.replace("143", "10.0.2.143"))

    if "174" in query and "192.168" not in query:
        variants.append(query.replace("174", "192.168.2.174"))

    if "175" in query and "192.168" not in query:
        variants.append(query.replace("175", "192.168.2.175"))

    # 3. 缩写展开
    abbreviations = {
        "dms": ["dms", "数据管理系统", "大屏"],
        "led": ["led", "大屏"],
        "gmos": ["gmos", "政务系统"]
    }

    for abbr, expansions in abbreviations.items():
        if abbr in query_lower:
            for exp in expansions:
                if exp != abbr:
                    variants.append(query.replace(abbr, exp))

    # 4. 去重并返回
    return list(dict.fromkeys(variants))  # 保持顺序的去重


# ==================== 新增：条目更新函数 ====================

def update_entry_in_place(
    kb_path: Path,
    entry_id: str,
    updates: dict[str, Any],
    *,
    lock_held: bool = False,
) -> bool:
    """原地更新条目（替换模式）。

    Args:
        kb_path: kb.jsonl 文件路径
        entry_id: 条目 ID
        updates: 要更新的字段字典

    Returns:
        是否找到并更新了条目
    """
    if not kb_path.exists():
        return False

    lock_context = nullcontext() if lock_held else bucket_lock(kb_path)
    with lock_context:
        entries = read_jsonl(kb_path)
        found = False

        for e in entries:
            if e.get("id") == entry_id:
                # 更新字段
                e.update(updates)
                # 保留创建时间；并发修订号由稳定语义内容计算。
                e["updated_ts"] = now_iso()
                try:
                    from kb_evidence import canonical_entry_revision

                    e["record_rev"] = canonical_entry_revision(e)
                except ImportError:
                    pass
                found = True
                break

        if found:
            _rewrite_jsonl(kb_path, entries, lock_held=True)

        return found


def mark_entry_as_milestone(
    kb_path: Path,
    entry_id: str,
    reason: str,
    *,
    lock_held: bool = False,
) -> bool:
    """标记条目为里程碑。

    Args:
        kb_path: kb.jsonl 文件路径
        entry_id: 条目 ID
        reason: 保留为里程碑的原因

    Returns:
        是否找到并更新了条目
    """
    return update_entry_in_place(kb_path, entry_id, {
        "state": "milestone",
        "milestone_reason": reason
    }, lock_held=lock_held)


def search_related_entries(kb_path: Path, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """搜索与新条目相关的旧条目。

    根据 feature_key 或 title 相似度查找可能需要更新的旧条目。

    Args:
        kb_path: kb.jsonl 文件路径
        entry: 新条目字典

    Returns:
        相关旧条目列表（按时间倒序）
    """
    if not kb_path.exists():
        return []

    entries = read_jsonl(kb_path)
    related = []

    # 检索条件
    new_feature_key = entry.get("feature_key")
    new_title = entry.get("title", "")
    new_kind = entry.get("kind")

    for e in entries:
        # 跳过已标记为 archived 或 deleted 的
        if e.get("state") == "archived" or e.get("_deleted"):
            continue

        # 条件 1：feature_key 匹配
        if new_feature_key and e.get("feature_key") == new_feature_key:
            related.append(e)
            continue

        # 条件 2：同 kind + 标题高度相似
        if new_kind and e.get("kind") == new_kind:
            old_title = e.get("title", "")
            if old_title and new_title:
                # 简单相似度判断（后续可优化）
                if old_title.lower() in new_title.lower() or new_title.lower() in old_title.lower():
                    related.append(e)

    # 按时间倒序排序（最新的在前）
    related.sort(key=lambda x: x.get("ts", ""), reverse=True)

    return related

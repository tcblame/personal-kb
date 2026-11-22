"""Read-only project suggestions from the query, workspace and evidenced vocabulary."""
from __future__ import annotations

import codecs
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import kb_domain
from kb_lib import _current_branch, _repo_root, expand_query
from kb_timeline import bounds, usable_events

# Titles and trigger terms supply the domain vocabulary. These are conversational
# glue and project-reference grammar, independent of which projects are installed.
WORDS = re.compile(r"[a-z][a-z0-9_.+-]*|[\u3400-\u9fff]+", re.I)
HAN = re.compile(r"^[\u3400-\u9fff]+$")
GLUE = re.compile(r"宝宝|然后|目前|有哪些|完成任务|怎么|什么|这个|那个|现在|可以|一下|是不是|如何|为什么|上面的?|之前|还有|帮我|需要|我们|这里|里面|那边|那些|能不能|有没有|的问题|的是|[的了和与及也把我你]")
COMMON = set("项目 系统 课程 平台 工具 模块 应用 仓库 工作台 当前 修改 问题 老师 学生 用户 功能 需求 代码 文件 目录 其他 重新 方案 架构 优化 一个 之前 目前 昨天 记录 需要 问题 今天 之前 开始 希望 继续 现在 有没有 这个 那个 所有 是否 完成 同时 内容 分析 方式 图片 使用 确认 效果 老师那边 主机 进行 可以 主要 已经".split())
SUBJECT = re.compile(r"([\u3400-\u9fffA-Za-z0-9_-]{2,18}?)(?:项目|课程|系统|平台|模块|助手|工作台|仓库)(?!上)")
DEICTIC = re.compile(r"这个项目|当前项目|本项目|这个仓库|当前仓库|本仓库|这份代码|this (?:project|repo)\b", re.I)
WORK = re.compile(r"修[复改好一]|实现|开发|部署|排查|打开|接入|上线|维护|联调|仓库|项目|改一下|查一下", re.I)
GENERAL = re.compile(r"是什么意思|怎么理解|怎么讲|怎么写开场|是什么原理|what (?:is|does)\b", re.I)
TEXT_EXT = set(".md .rst .txt .py .go .rs .js .ts .tsx .jsx .vue .java .kt .cs .cpp .c .h .sql .html .yaml .yml .toml".split())
MARKERS = ("README.md", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")
MAX_FILES = 4


@dataclass
class Project:
    repo: str
    label: str
    project_id: str = ""
    aliases: list[str] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    children: list[dict] = field(default_factory=list)
    roots: set[Path] = field(default_factory=set)
    rows: list[dict] = field(default_factory=list)
    locations: dict[str, Path] = field(default_factory=dict)
    files: list[Path] = field(default_factory=list)


def _catalog(graph, scope):
    projects = {}
    owners = defaultdict(list)
    entities = Counter(e["domain"]["entity_id"] for e in graph["entries"])
    current = {e["domain"]["entity_id"]: e for e in graph["entries"]
               if entities[e["domain"]["entity_id"]] == 1 and kb_domain.active(e, scope)}
    declared_roots = defaultdict(list)
    for entry in current.values():
        d = entry["domain"]
        key = d["project_id"]
        if d["entity_type"] != "project":
            continue
        names = entry.get("aliases", [])
        project = Project(entry["repo"], names[0] if names else entry["repo"], key, list(names))
        active_names = {}
        for event in usable_events(d.get("observations", [])):
            if event["field"] not in {"alias", "project_alias"} or event["state"] != "confirmed":
                continue
            start = bounds(event["date"])[0]
            if start <= datetime.now(timezone.utc):
                active_names.setdefault(event["value"], []).append((start, event["basis"] == "ended"))
        project.spoken = [name for name, history in active_names.items() if not max(history)[1]]
        workspace = Path(entry.get("workspace_dir") or ".")
        # Rows also have a bookkeeping cwd. Require a project-owned local source
        # or the repository's own directory before using it as project identity.
        if workspace.is_absolute() and (workspace.name.casefold() == project.repo.split("/")[-1].casefold() or
                any(Path(f).is_absolute() and Path(f).is_relative_to(workspace) for f in entry.get("source_paths", []))):
            project.roots.add(workspace)
            declared_roots[(workspace, project.repo.split("/")[-1].casefold())].append(key)
        projects[key] = project
        owners[entry["repo"]].append(key)
    for entry in current.values():
        d = entry["domain"]
        parent = projects.get(d["project_id"])
        if (parent and d["entity_type"] in {"component", "repository"} and any(
                edge["type"] == "belongs_to" and edge["target"] == parent.project_id
                and kb_domain.declaration_active(edge, scope) for edge in d.get("relations", []))):
            parent.children.append(entry)
    for row in graph["routing_rows"]:
        repo = row["repo"]
        workspace = Path(row.get("workspace_dir") or ".")
        keys = owners[repo] or declared_roots[(workspace, repo.split("/")[-1].casefold())]
        key = row.get("project_id") or (keys[0] if len(keys) == 1 else repo)
        if row.get("entity_id") and (row["entity_id"] not in current or key not in projects):
            continue
        if scope.get("branch") and row.get("branch") != scope["branch"]:
            continue
        p = projects.setdefault(key, Project(repo, repo))
        p.rows.append(row)
        if repo != p.repo and repo not in p.aliases:
            p.aliases.append(repo)
        # A session can edit someone else's project from a shared working
        # directory. Its cwd alone must never claim ownership of that directory.
        if workspace.is_absolute() and workspace.name.casefold() == repo.split("/")[-1].casefold():
            p.roots.add(workspace)
    return projects


def _root(path, projects):
    known = {r for p in projects.values() for r in p.roots if path.is_relative_to(r)}
    root = _repo_root(path)
    # Respect a nested checkout or a separately identified nested project.
    if root:
        known.add(root)
        return max(known, key=lambda r: len(r.parts))
    ancestor = max(known, key=lambda r: len(r.parts), default=None)
    for directory in (path, *path.parents):
        if directory == ancestor:
            return ancestor
        if directory == Path.home() or directory.parent == directory:
            break
        markers = MARKERS[1:] if ancestor else MARKERS
        if any((directory / marker).is_file() for marker in markers):
            return directory
    return ancestor


def _attach(projects, path, kind, supplied_file=None):
    root = _root(path, projects)
    if not root and supplied_file is not None and path not in {Path.home(), Path(path.anchor)}:
        root = path
    if not root:
        return
    matches = [p for p in projects.values() if root in p.roots]
    if not matches:
        repo = root.name.casefold()
        matches = [projects.setdefault(("workspace", root), Project(repo, root.name))]
    for p in matches:
        p.roots.add(root)
        p.locations[kind] = root
        if supplied_file is not None:
            p.files.append(supplied_file)


def _read_project_text(path, root):
    if (path.suffix.casefold() not in TEXT_EXT or not path.is_relative_to(root)
            or any(part.startswith(".") for part in path.relative_to(root).parts)
            or path.is_symlink() or not path.is_file()):
        return ""
    try:
        with path.open("rb") as stream:
            data = stream.read(32768)
        if b"\0" not in data:
            return codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=False).casefold()
    except (UnicodeError, OSError):
        pass
    return ""


def _mentions(text, name):
    name = name.strip().casefold()
    if len(name) < 2:
        return False
    if name.isascii():
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])", text))
    return name in text


def _terms(query):
    result = set()
    for word in WORDS.findall(GLUE.sub(" ", query)):
        if HAN.match(word):
            result.update(word[i:i+n] for n in range(2, min(7, len(word))+1) for i in range(len(word)-n+1))
        elif len(word) > 1:
            result.add(word.strip("._+-"))
    return {w for w in result if len(w) >= 2 and w not in COMMON}


def _features(project, terms, overview=None):
    hits = {}
    def collect(text, pointer, exact=()):
        for term in terms:
            if _mentions(text, term):
                weight = {2: 1, 3: 2}.get(len(term), 3)
                if term in exact:
                    weight += 2
                if weight > hits.get(term, (0, ""))[0]:
                    hits[term] = (weight, pointer)
    for row in project.rows:
        vocabulary = [row["title"], *row.get("aliases", []), *row.get("trigger_terms", [])]
        terms_here = [v.casefold() for v in vocabulary]
        collect("\n".join(terms_here), row["id"], set(terms_here))
    for path in project.files:
        root = next((r for r in project.roots if path.is_relative_to(r)), None)
        if root:
            collect(path.stem.casefold()+"\n"+_read_project_text(path, root), str(path))
    roots = set(project.locations.values())
    if overview:
        roots.add(overview)
    for root in sorted(roots):
        readme = root / "README.md"
        text = _read_project_text(readme, root)
        if text:
            collect(text, str(readme))
    return hits


CHAT_MARKDOWN = re.compile(r"\[([^\]\n]+)\]\(codex://(?:threads?|chats?)/[0-9a-f-]{36}(?:\?[^)\s]*)?\)", re.I)


def chat_titles(query):
    titles = []
    def title(match):
        label = re.split(r"\s+·\s+(?:WSL|SSH|Local)\b", match[1], flags=re.I)[0]
        titles.append(label)
        return label
    return CHAT_MARKDOWN.sub(title, query), titles


def suggest(query, graph, *, cwd, files=(), scope=None, synonyms=None, verify):
    """Return a user decision with provenance. Never write or assert graph edges."""
    if len(files) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} current files can be supplied")
    projects = _catalog(graph, scope or {})
    if cwd:
        _attach(projects, cwd.resolve(), "cwd")
    for name in files:
        if re.match(r"^[a-zA-Z]:[\\/]", str(name)) and shutil.which("wslpath"):
            converted = subprocess.run(["wslpath", "-u", str(name)], capture_output=True, text=True)
            if converted.returncode:
                continue
            name = converted.stdout.strip()
        path = Path(name)
        if not path.is_absolute():
            path = (cwd or Path.cwd()) / path
        if path.is_file():
            resolved = path.resolve()
            _attach(projects, resolved.parent, "current_file", resolved)
    if scope and scope.get("repo_id"):
        requested = scope["repo_id"]
        projects = {k: p for k, p in projects.items() if requested in (p.repo, *p.aliases)
                    or Path(requested) in p.roots}
    clean, link_titles = chat_titles(query)
    original_query = re.sub(r"(?:https?|codex)://[^\s)]+", " ", clean).strip()
    if len(original_query) > 2048:
        original_query = original_query[:1024] + " " + original_query[-1024:]
    query = original_query.casefold()
    title_terms = _terms(" ".join(link_titles).casefold()) - {"ai", "wsl"}
    names = {}
    masked = query
    for key, p in projects.items():
        child_names = [name for child in p.children for name in [*child.get("aliases", []), child["title"]]]
        strong = [n for n in [p.project_id, p.repo, *p.aliases, *child_names] if _mentions(query, n)]
        spoken = [n for n in p.spoken if _mentions(query, n)]
        names[key] = (strong, spoken)
        for name in strong + spoken:
            masked = masked.replace(name.casefold(), " ")
    terms = _terms(masked)
    # A fragment of a confirmed nickname can point to a foreign project overview.
    # It can only invite a comparison. The file still has to support the task.
    partial = {k: any(len(t) >= 2 and HAN.match(t) and name.casefold().startswith(t)
                      for t in terms for name in [*p.aliases, *p.spoken] if name.casefold() != t)
               for k, p in projects.items()}
    remote = [k for k, p in projects.items() if partial[k] and p.project_id and not p.locations and p.roots]
    overviews = {k: sorted(projects[k].roots)[0] for k in remote[:4]}
    hits = {k: _features(p, terms, overviews.get(k)) for k, p in projects.items()}
    dictionary = synonyms or {}
    spellings = {v.casefold() for head, values in dictionary.items() for v in [head, *values]}
    expanded = {t: set(expand_query(t, dictionary)) - {t} for t in sorted(spellings) if _mentions(query, t)}
    freq = Counter(term for found in hits.values() for term in found)
    subjects = [m[1] for m in SUBJECT.finditer(GLUE.sub(" ", masked)) if m[1] not in COMMON]
    short_subject = bool(subjects and len(query) < 12)
    general = bool(GENERAL.search(query))
    ranked = []
    for key, p in projects.items():
        strong, spoken = names[key]
        exact_spoken = bool(spoken and (query.strip("。？！?! ") in {n.casefold() for n in spoken} or WORK.search(query)))
        evidence = []
        weight = 0
        spans = []
        for term, (base, source) in sorted(hits[key].items(), key=lambda t: (-(t[1][0] / freq[t[0]]**0.6), -len(t[0]), t[0])):
            start = masked.find(term)
            if start < 0 or any(start < b and a < start + len(term) for a, b in spans) or any(term in e["term"] or e["term"] in term for e in evidence):
                continue
            spans.append((start, start + len(term)))
            evidence.append({"term": term, "source": source})
            weight += base / freq[term]**0.6
            if len(evidence) == 5:
                break
        local = bool(p.locations)
        named_subject = next((s for s in subjects if any(row["title"].casefold().startswith(s)
                                                          for row in p.rows)), "")
        alternatives = {n.casefold() for n in [p.repo, *p.aliases, *p.spoken]}
        lexical = next(((t, sorted(v & alternatives)[0]) for t, v in expanded.items() if v & alternatives), None)
        named = bool(strong or exact_spoken)
        deictic = bool(local and DEICTIC.search(query))
        feature = weight >= 2.5
        if not (named or deictic or feature or (lexical and not general) or ((local and short_subject or spoken) and not general)):
            continue
        if general and not strong and not deictic and weight < 4:
            continue
        candidate = _candidate(p, evidence)
        if lexical and not (strong or spoken):
            candidate["dictionary_match"] = {"term": lexical[0], "spelling": lexical[1]}
        linked_terms = [f["term"] for f in evidence if f["term"] in title_terms]
        if linked_terms:
            candidate["linked_title_terms"] = linked_terms
        if strong or spoken:
            candidate["matched_name"] = (strong or spoken)[0]
        if named_subject:
            candidate["subject"] = named_subject
            if not p.project_id and p.label == p.repo:
                candidate["label"] = f"{named_subject}（{p.repo}）"
        score = weight + 2 * bool(lexical) + 1.5 * bool(partial[key] and feature) + 3 * local + 2 * bool(p.files) + 6 * deictic + 5 * bool(named_subject) + 18 * named
        if local and linked_terms and weight >= 4 and len(evidence) >= 2:
            score += 3
        ranked.append((score, candidate, named, deictic, named_subject, weight))
    if not ranked:
        return {}
    ranked.sort(key=lambda row: (-row[0], row[1]["label"]))
    score, top, named, deictic, named_subject, weight = ranked[0]
    credible = [r for r in ranked[1:] if r[2] or r[0] >= max(3, score - 3)]
    # A visible local workspace can conflict with a highly specific foreign
    # feature, even when the local project's own KB has little vocabulary.
    foreign = not top["locations"]
    if foreign and not (named or named_subject) and not (scope or {}).get("repo_id"):
        for p in projects.values():
            if p.locations and not any(r[1].get("project_id", r[1]["repo"]) == (p.project_id or p.repo) for r in credible):
                credible.append((0, _candidate(p, []), False, False, False, 0))
    unfamiliar_subject = bool(subjects and not (named or named_subject or deictic))
    use = not credible and not unfamiliar_subject and (named or deictic or (named_subject and weight >= 4)
          or (not foreign and weight >= 4 and len(top["features"]) >= 2))
    candidates = [top] if use else [top, *(r[1] for r in credible[:2])]
    if len(candidates) == 1 and top.get("project_id"):
        owner = next(e for e in graph["entries"] if e["domain"]["entity_id"] == top["project_id"])
        if verify(owner).get("state") != "fresh":
            top["needs_recheck"] = True
            use = False
        else:
            children, explicit = _children(query, projects[top["project_id"]].children, top, verify)
            if children:
                if re.search(r"有哪些|列出|列举|列一下", query) and not explicit:
                    top["members"] = children
                else:
                    candidates = children
                    use = explicit and len(children) == 1 and not children[0].get("needs_recheck")
    if use:
        return {"state": "use", "candidates": candidates}

    labels = "、".join(c["label"] for c in candidates)
    subject = next((m[0] for m in SUBJECT.finditer(original_query) if m[1] in subjects), "")
    prefix = f"你说的“{subject}”指" if subject else "你指的是"
    other = "另一门独立课程" if subject.endswith("课程") else "其他项目或独立话题"
    return {"state": "clarify", "candidates": candidates, "question": f"{prefix}{labels}，还是{other}？"}


def _candidate(project, evidence):
    locations = {k: str(v) for k, v in project.locations.items()}
    if project.files:
        locations["current_file"] = str(project.files[0])
    result = {"repo": project.repo, "label": project.label, "locations": locations, "features": evidence}
    if project.project_id:
        result["project_id"] = project.project_id
    return result


def _children(query, children, parent, verify):
    """Narrow a family only on a named child, a feature or a request for variants."""
    enumerate_family = re.search(r"哪[个些一](?:个)?(?:子项目|分部|站点|版本|区域|套)|列[出举].{0,5}子项目", query)
    matches = []
    for entry in children:
        d = entry["domain"]
        names = [*entry.get("aliases", []), entry["title"]]
        name = next((n for n in names if _mentions(query, n)), "")
        words = [t for t in entry.get("trigger_terms", []) if len(t) >= 3 and t not in COMMON
                 and t not in parent["label"] and _mentions(query, t)]
        if name or words or enumerate_family:
            matches.append((name, words, entry))
    explicit = any(name for name, _, _ in matches)
    if explicit:
        matches = [m for m in matches if m[0]]
    matches.sort(key=lambda m: (-len(m[1]), m[2]["title"]))
    result = []
    for name, words, entry in matches[:3]:
        d = entry["domain"]
        child = {**parent, "entity_id": d["entity_id"], "label": name or (entry.get("aliases") or [entry["title"]])[0],
                 "parent_label": parent["label"], "relation_source": entry["id"]}
        if words:
            child["features"] = [{"term": w, "source": entry["id"]} for w in words[:3]]
        if d.get("repo_id"):
            child["repo"] = d["repo_id"]
        if verify(entry).get("state") != "fresh":
            child["needs_recheck"] = True
        result.append(child)
    return result, explicit


def search_branch(chosen, graph):
    location = chosen["locations"].get("current_file") or chosen["locations"].get("cwd")
    if location:
        cwd = Path(location)
        root = _repo_root(cwd if cwd.is_dir() else cwd.parent)
        if root:
            return _current_branch(root) or ""
    branches = {row.get("branch", "") for row in graph["routing_rows"] if row["repo"] == chosen["repo"]}
    return branches.pop() if len(branches) == 1 else ""

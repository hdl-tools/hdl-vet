"""Phase 5c, part 3: the release workflow. Rules and the tests that hold them.

A release is made from a version tag. The workflow runs every check again on the
tagged commit, builds the packages and the PDF of the manual, publishes to PyPI
after the owner approves, and then makes the GitHub release.

Owner decisions (2026-11-17)

- The name on PyPI is `hdl-vet` (the owner checked it). The command stays `vet`,
  the import stays `hdl_vet`.
- The first release is 0.1.0.
- A trial run can be started by hand; it builds everything and uploads to
  TestPyPI only.
- Publishing to PyPI waits for the owner's approval: the job runs in the GitHub
  environment `pypi`, which has a required reviewer. The environment and the
  trusted publishers on PyPI and TestPyPI are set up by the owner; the pull
  request gives the exact values.

RULES THE TESTS CHECK

P1  The version is 0.1.0 in `pyproject.toml`, in `hdl_vet.__version__` and in
    `docs/manual/version.tex`.
P2  `.github/scripts/check_version.py [TAG]` exits with status 0 when those
    three versions are equal and, if TAG is given, TAG is "v" followed by that
    version. Otherwise it prints one line that says what differs and exits with
    status 1. It needs nothing but the Python standard library, and it does not
    import `hdl_vet` (it runs before anything is installed).
P3  `.github/workflows/release.yml` starts on a pushed tag `v*` and by hand
    (`workflow_dispatch`). It has these jobs:
      version   runs check_version.py; with the tag name when a tag started it
      ci        calls ci.yml, so every check of a pull request runs on the commit
      manual    calls manual.yml, which uploads the artifact `hdl-vet-manual`
      build     `uv build` (source package and wheel) and uploads `dist`
      testpypi  only when started by hand: uploads `dist` to TestPyPI
      pypi      only for a tag: uploads `dist` to PyPI
      release   only for a tag, after `pypi`: the GitHub release with the wheel,
                the source package and the PDF as `hdl-vet-manual-<version>.pdf`
    `testpypi` and `pypi` need version, ci, manual and build: nothing is
    uploaded unless everything passed.
P4  Publishing uses trusted publishing: `pypa/gh-action-pypi-publish`, the
    permission `id-token: write` on the two publish jobs only, the environment
    `pypi` on the job `pypi` and `testpypi` on the job `testpypi`. No token and
    no password: the workflow reads no secret. The trial upload gives
    `repository-url: https://test.pypi.org/legacy/` and `skip-existing: true`
    (a version can be uploaded to TestPyPI only once; a second trial of the same
    version still checks the build and the publisher).
P5  Least privilege. The workflow's default permission is `contents: read`.
    Only the job `release` has `contents: write`. No job has both `id-token:
    write` and `contents: write`.
P6  The GitHub release is made with `gh release create` and `--verify-tag`, for
    the tag that started the workflow, and never when the workflow was started
    by hand.
P7  `ci.yml` can be called by another workflow (`workflow_call`) and behaves as
    before for pull requests and pushes.
P8  Only these actions are used in release.yml: actions/checkout,
    actions/upload-artifact, actions/download-artifact, astral-sh/setup-uv,
    pypa/gh-action-pypi-publish.
P9  `pyproject.toml` has what the page on PyPI shows: `[project.urls]` with
    "Repository", keywords, and classifiers for Python 3.11 and 3.13 and for the
    topic. The licence is given by the `license` field; a licence classifier
    is deprecated and is not added. It names no person.
P10 CONTRIBUTING.md has a section "Make a release" that a maintainer can follow:
    change the version in the three places, start a trial run, push the tag,
    approve the upload. It names `check_version.py`, `release.yml`, TestPyPI and
    the environment `pypi`. The language rules hold (tests/test_readme.py checks
    the whole file).

LEFT OPEN

- The README still says to install from a clone. It changes to
  `pip install hdl-vet` in a small pull request after the first release exists,
  so that it never names a package that is not there.
- The README's relative links do not work on the PyPI page.
- Actions are pinned by tag, not by commit hash.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import hdl_vet

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
CHECK = REPO / ".github" / "scripts" / "check_version.py"
VERSION = "0.1.0"


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _jobs() -> dict[str, str]:
    """The text of each job of release.yml, keyed by its name (jobs are indented by two spaces)."""
    text = RELEASE.read_text(encoding="utf-8")
    body = text[text.index("\njobs:") + len("\njobs:") :]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):[ \t]*$", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def _head() -> str:
    text = RELEASE.read_text(encoding="utf-8")
    return text[: text.index("\njobs:")]


def _needs(job: str) -> set[str]:
    inline = re.search(r"^    needs:[ \t]*\[([^\]]*)\]", job, re.M)
    if inline:
        return {n.strip() for n in inline.group(1).split(",") if n.strip()}
    one = re.search(r"^    needs:[ \t]*([A-Za-z0-9_-]+)[ \t]*$", job, re.M)
    if one:
        return {one.group(1)}
    block = re.search(r"^    needs:[ \t]*\n((?:      - .*\n)+)", job, re.M)
    return {line.strip()[2:].strip() for line in block.group(1).splitlines()} if block else set()


# --- P1, P2 -------------------------------------------------------------------------------------


def test_the_version_is_the_first_release():
    assert hdl_vet.__version__ == VERSION
    assert _pyproject()["project"]["version"] == VERSION
    assert VERSION in (REPO / "docs" / "manual" / "version.tex").read_text(encoding="utf-8")


def _check(*args: str, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(CHECK), *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def test_check_version_accepts_the_right_tag():
    assert _check().returncode == 0
    done = _check("v" + VERSION)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("tag", [VERSION, "v0.1.1", "v0.1", "v0.1.0rc1", "release-0.1.0", "V0.1.0", ""])
def test_check_version_refuses_another_tag(tag):
    done = _check(tag)
    assert done.returncode == 1
    lines = (done.stdout + done.stderr).strip().splitlines()
    assert len(lines) == 1, lines
    assert VERSION in lines[0]
    assert "Traceback" not in lines[0]


def test_check_version_sees_a_version_that_differs(tmp_path: Path):
    """In a copy of the three files, with one of them changed, the script says which one."""
    for relative in (
        "pyproject.toml",
        "src/hdl_vet/__init__.py",
        "docs/manual/version.tex",
        ".github/scripts/check_version.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / relative).read_bytes())
    copy = tmp_path / ".github" / "scripts" / "check_version.py"

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(copy)], cwd=tmp_path, capture_output=True, text=True, check=False
        )

    assert run().returncode == 0
    for relative in ("pyproject.toml", "src/hdl_vet/__init__.py", "docs/manual/version.tex"):
        path = tmp_path / relative
        good = path.read_bytes()
        path.write_bytes(good.replace(VERSION.encode(), b"0.1.1"))
        done = run()
        assert done.returncode == 1, relative
        assert Path(relative).name in done.stdout + done.stderr, relative
        path.write_bytes(good)
    assert run().returncode == 0


def test_check_version_needs_only_the_standard_library():
    text = CHECK.read_text(encoding="utf-8")
    imported = set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.M))
    assert imported <= {"__future__", "pathlib", "re", "sys", "tomllib"}


# --- P3 -----------------------------------------------------------------------------------------


def test_the_triggers():
    head = _head()
    assert re.search(r"^on:", head, re.M)
    assert re.search(r"^  push:\s*\n    tags:\s*\n?\s*(?:- )?\[?[\"']?v\*", head, re.M)
    assert "branches:" not in head
    assert "workflow_dispatch" in head
    assert "pull_request" not in head


def test_the_jobs_and_what_they_wait_for():
    jobs = _jobs()
    assert set(jobs) == {"version", "ci", "manual", "build", "testpypi", "pypi", "release"}
    everything = {"version", "ci", "manual", "build"}
    assert _needs(jobs["testpypi"]) == everything
    assert _needs(jobs["pypi"]) == everything
    assert "pypi" in _needs(jobs["release"])
    assert "check_version.py" in jobs["version"]
    assert "github.ref_name" in jobs["version"]
    assert "uses: ./.github/workflows/ci.yml" in jobs["ci"]
    assert "uses: ./.github/workflows/manual.yml" in jobs["manual"]
    assert "uv build" in jobs["build"]
    assert "--wheel" not in jobs["build"]
    assert "name: dist" in jobs["build"]


def test_when_each_upload_runs():
    jobs = _jobs()
    by_hand = re.search(r"^    if:.*github\.event_name == 'workflow_dispatch'", jobs["testpypi"], re.M)
    assert by_hand
    for name in ("pypi", "release"):
        condition = re.search(r"^    if:(.*)$", jobs[name], re.M)
        assert condition, name
        assert "refs/tags/v" in condition.group(1), name
        assert "workflow_dispatch" not in condition.group(1), name


# --- P4, P5 -------------------------------------------------------------------------------------


def test_publishing_is_trusted_publishing():
    jobs = _jobs()
    text = RELEASE.read_text(encoding="utf-8")
    for name, environment in (("pypi", "pypi"), ("testpypi", "testpypi")):
        job = jobs[name]
        assert "pypa/gh-action-pypi-publish@" in job, name
        assert re.search(r"^    environment:\s*(?:\n      name:\s*)?" + environment + r"\s*$", job, re.M), name
        assert re.search(r"^      id-token: write\s*$", job, re.M), name
        assert "name: dist" in job, name
    assert "repository-url: https://test.pypi.org/legacy/" in jobs["testpypi"]
    assert "skip-existing: true" in jobs["testpypi"]
    assert "repository-url" not in jobs["pypi"]
    assert "skip-existing" not in jobs["pypi"]
    for forbidden in ("secrets.", "password:", "API_TOKEN", "TWINE_"):
        assert forbidden not in text, forbidden


def test_least_privilege():
    jobs = _jobs()
    assert re.search(r"^permissions:\s*\n  contents: read\s*$", _head(), re.M)
    assert "id-token" not in _head()
    for name, job in jobs.items():
        token = "id-token: write" in job
        write = "contents: write" in job
        assert token == (name in ("pypi", "testpypi")), name
        assert write == (name == "release"), name
    assert "write-all" not in RELEASE.read_text(encoding="utf-8")


# --- P6 -----------------------------------------------------------------------------------------


def test_the_github_release():
    job = _jobs()["release"]
    assert "gh release create" in job
    assert "--verify-tag" in job
    assert "github.ref_name" in job
    assert "hdl-vet-manual-" in job
    assert ".pdf" in job
    assert "name: hdl-vet-manual" in job
    assert "name: dist" in job
    assert "github.token" in job or "GITHUB_TOKEN" in job


# --- P7, P8 -------------------------------------------------------------------------------------


def test_ci_can_be_called_and_is_otherwise_as_before():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    head = text[: text.index("\njobs:")]
    for needed in ("workflow_call", "pull_request", "workflow_dispatch", "branches: [main]"):
        assert needed in head, needed
    for job in ("lint:", "test:", "package:"):
        assert re.search(r"^  " + job, text, re.M), job
    assert "HDL_VET_REQUIRE_CORPUS" in text
    manual = (WORKFLOWS / "manual.yml").read_text(encoding="utf-8")
    assert "workflow_call" in manual


def test_only_known_actions_are_used():
    used = set(re.findall(r"uses:\s*([^@\s]+)@", RELEASE.read_text(encoding="utf-8")))
    assert used <= {
        "actions/checkout",
        "actions/upload-artifact",
        "actions/download-artifact",
        "astral-sh/setup-uv",
        "pypa/gh-action-pypi-publish",
    }
    assert "pypa/gh-action-pypi-publish" in used


# --- P9, P10 ------------------------------------------------------------------------------------


def test_the_metadata_for_the_package_index():
    project = _pyproject()["project"]
    assert project["name"] == "hdl-vet"
    assert project["urls"]["Repository"].startswith("https://github.com/")
    assert len(project["keywords"]) >= 3
    classifiers = set(project["classifiers"])
    assert "Programming Language :: Python :: 3.11" in classifiers
    assert "Programming Language :: Python :: 3.13" in classifiers
    assert any(c.startswith("Topic :: ") for c in classifiers)
    assert "authors" not in project
    assert "maintainers" not in project
    assert project["scripts"] == {"vet": "hdl_vet.cli:main"}


def test_contributing_says_how_to_make_a_release():
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert re.search(r"^## Make a release\s*$", text, re.M)
    section = text[text.index("## Make a release") :]
    section = section[: section.index("\n## ", 5)] if "\n## " in section[5:] else section
    for needed in (
        "pyproject.toml",
        "src/hdl_vet/__init__.py",
        "docs/manual/version.tex",
        "check_version.py",
        "release.yml",
        "TestPyPI",
        "`pypi`",
        "git tag",
    ):
        assert needed in section, needed
    assert re.search(r"^1\. ", section, re.M)


# --- from the review of the workflow ------------------------------------------------------------
#
# P11 A release tag names a commit of `main`: the job `version` fetches the whole
#     history and stops unless the tagged commit is an ancestor of `origin/main`.
#     Otherwise a tag on any commit, with any release.yml, would reach the upload.
# P12 check_version.py reads a version only when the file names exactly one, at
#     the start of a line (a second assignment, or one in a comment or a
#     docstring, is a failure, not a guess), and accepts only the form X.Y.Z with
#     plain numbers.
# P13 The two actions that build and publish the packages are pinned by commit
#     hash in release.yml, with the version in a comment. The release build uses
#     no cache. No checkout in any workflow leaves its token in the clone.
# P14 "Make a release" says where the approval is given, tells the maintainer to
#     push one tag by name, and says what to do after a failed release.


def _copy_for_check(tmp_path: Path) -> Path:
    for relative in (
        "pyproject.toml",
        "src/hdl_vet/__init__.py",
        "docs/manual/version.tex",
        ".github/scripts/check_version.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / relative).read_bytes())
    return tmp_path


def _run_copy(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    script = root / ".github" / "scripts" / "check_version.py"
    return subprocess.run(
        [sys.executable, "-I", str(script), *args], cwd=root, capture_output=True, text=True, check=False
    )


def test_the_tag_must_be_on_main():
    """P11."""
    job = _jobs()["version"]
    assert "fetch-depth: 0" in job
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in job
    step = job[job.index("git merge-base") - 200 : job.index("git merge-base")]
    assert "if: github.event_name == 'push'" in step


@pytest.mark.parametrize(
    "relative, old, new",
    [
        ("src/hdl_vet/__init__.py", '__version__ = "0.1.0"', '__version__ = "0.1.0"\n__version__ = "0.2.0"'),
        ("src/hdl_vet/__init__.py", '__version__ = "0.1.0"', '__version__ = "0.1.0"\n\n__version__ = "0.1.0"'),
        (
            "docs/manual/version.tex",
            "\\newcommand{\\hdlvetversion}{0.1.0}",
            "% \\newcommand{\\hdlvetversion}{0.1.0}\n\\newcommand{\\hdlvetversion}{0.2.0}",
        ),
        (
            "docs/manual/version.tex",
            "\\newcommand{\\hdlvetversion}{0.1.0}",
            "\\newcommand{\\hdlvetversion}{0.1.0}\n\\newcommand{\\hdlvetversion}{0.1.0}",
        ),
    ],
    ids=["two-assignments", "the-same-twice", "a-comment-in-tex", "twice-in-tex"],
)
def test_check_version_does_not_guess_which_line_counts(tmp_path: Path, relative, old, new):
    """P12."""
    root = _copy_for_check(tmp_path)
    assert _run_copy(root).returncode == 0
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
    done = _run_copy(root)
    assert done.returncode == 1
    assert len((done.stdout + done.stderr).strip().splitlines()) == 1
    assert Path(relative).name in done.stdout + done.stderr


@pytest.mark.parametrize("version", ["banana", "0.1.0+abc", " 0.1.0", "0.1", "0.1.0.0", "01.1.0", "0.1.0rc1", "v0.1.0"])
def test_check_version_accepts_only_three_plain_numbers(tmp_path: Path, version):
    """P12. The three files agree, but on something that is not X.Y.Z."""
    root = _copy_for_check(tmp_path)
    for relative in ("pyproject.toml", "src/hdl_vet/__init__.py", "docs/manual/version.tex"):
        path = root / relative
        text = path.read_text(encoding="utf-8")
        assert text.count(VERSION) == 1, relative
        path.write_bytes(text.replace(VERSION, version).encode("utf-8"))
    for args in ((), ("v" + version,)):
        done = _run_copy(root, *args)
        assert done.returncode == 1, (version, args)
        assert "Traceback" not in done.stdout + done.stderr


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0", "10.20.30", "0.0.0"])
def test_check_version_accepts_a_plain_version(tmp_path: Path, version):
    root = _copy_for_check(tmp_path)
    for relative in ("pyproject.toml", "src/hdl_vet/__init__.py", "docs/manual/version.tex"):
        path = root / relative
        path.write_bytes(path.read_text(encoding="utf-8").replace(VERSION, version).encode("utf-8"))
    assert _run_copy(root, "v" + version).returncode == 0
    assert _run_copy(root, "v" + version + " ").returncode == 1


def test_the_publishing_actions_are_pinned_and_nothing_is_cached():
    """P13."""
    text = RELEASE.read_text(encoding="utf-8")
    for action in ("pypa/gh-action-pypi-publish", "astral-sh/setup-uv"):
        uses = re.findall(r"uses:\s*" + re.escape(action) + r"@(\S+)(.*)", text)
        assert uses, action
        for ref, rest in uses:
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (action, ref)
            assert re.search(r"#\s*v?\d+\.\d+", rest), (action, rest)
    assert "enable-cache: false" in _jobs()["build"]
    for name in ("release.yml", "ci.yml", "manual.yml"):
        workflow = (WORKFLOWS / name).read_text(encoding="utf-8")
        checkouts = len(re.findall(r"uses:\s*actions/checkout@", workflow))
        assert checkouts >= 1, name
        assert workflow.count("persist-credentials: false") == checkouts, name


def test_contributing_says_how_to_approve_and_how_to_recover():
    """P14."""
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = text[text.index("## Make a release") :]
    for needed in ("Review deployments", "git push origin v", "Re-run failed jobs", "git fetch origin", "main"):
        assert needed in section, needed
    assert "git push --tags" not in section


# P15 (owner, 2026-11-17) `.github/dependabot.yml` keeps the pinned actions current:
#     the ecosystem `github-actions` only, weekly, all actions in one pull request.
#     Python packages are not listed; their versions are pinned on purpose.


def test_dependabot_watches_the_actions_only():
    text = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "version: 2" in text
    assert re.findall(r"package-ecosystem:\s*(\S+)", text) == ["github-actions"]
    assert re.search(r"directory:\s*/\s*$", text, re.M)
    assert re.search(r"interval:\s*weekly", text)
    assert "groups:" in text

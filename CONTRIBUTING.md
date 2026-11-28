# Contributing to hdl-vet

This file tells you how to change hdl-vet. The user manual in `docs/manual` describes what the tool does. The README gives a short overview.

## Before you start

Before you start a larger change, open an issue. A larger change adds an operation, an option or a check. Say what you want to change and why. A maintainer can then tell you if the change fits the project.

Read the chapter "Limits and known problems" of the manual. It lists what hdl-vet does not do. You can fix some of these problems.

The code is in `src/hdl_vet`. The tests are in `tests`. Each test file has a docstring at its top that states the rules the tests check. Find the test file whose docstring states the rules of the code that you change. Read that docstring.

The package has these parts:

| Part of `src/hdl_vet` | What it holds |
| --- | --- |
| `cli.py`, `project.py` | the command line and the project file |
| `filelist.py`, `splice.py`, `listedit.py`, `paths.py` | the file list, the edits of text, small helpers |
| `session.py`, `signoff.py`, `output.py`, `archive.py` | the session, the sign-off, the output tree, saved sessions |
| `equiv.py`, `equiv_inert.py`, `prove.py`, `enables.py` | the equivalence check with Yosys, its checks of a child that stays a black box, the proof of the logic operations at the sign-off, and the way of each enable up to the top |
| `ops/` | the scripting interface |
| `slang_adapter/` | all that reads the design with pyslang |

The package `ops` has one module for each group:

| Module of `ops/` | What it holds |
| --- | --- |
| `vet.py` | the class `Vet`: the session, checked operations, groups |
| `module.py` | the class `Module`: ports, parameters, wires, assigns, removals |
| `args.py` | the checks of the arguments that are not names |
| `override.py` | `override_net`, the operation that changes logic |
| `insert_cell.py` | `insert_cell_on_net`, a cell on a net; it shares the code of `override.py` |
| `replace_expr.py` | `replace_expr`, another expression for a continuous assign; it uses the loop guard of `override.py` |
| `instance.py` | the class `Instance`: connections, parameter values, `replace_module` |
| `hier.py` | `punch_port` and `propagate_param` |
| `scope.py` | the declared scope |
| `raw.py` | `splice`, `text`, `expect`, `permit`, the gate and the mark of the logic calls |
| `plans.py` | `add_file` and the plans of an edit |
| `checks.py` | the post-conditions that the sign-off checks |
| `common.py`, `text.py` | the errors, the records, helpers for text and numbers |
| `host.py` | types only, for the type checker |

The package `slang_adapter` is the only place that imports `pyslang`:

| Module of `slang_adapter/` | What it holds |
| --- | --- |
| `api.py` | `load`, `parameter_names`, `instance_parameters`, `check_expression`, `is_wiring` and the readers of the names of a value |
| `design.py` | the class `Design` |
| `loading.py` | the load cache and the loader |
| `model.py` | modules and instances |
| `snapshots.py` | the structure of the design, for the comparison of the sign-off |
| `diagnostics.py` | diagnostics, drivers, signals that are undriven or unused |
| `hierrefs.py` | hierarchical references, the drivers and the constants of a signal |
| `pins.py` | the connection of a pin on every elaborated instance of a module |
| `records.py`, `text.py` | the data classes, helpers for tokens and identifiers |
| `host.py` | types only, for the type checker |

The tests are in folders by area:

| Folder of `tests/` | What it tests |
| --- | --- |
| `core` | the file list, splices, the adapter, the session, the sign-off |
| `ops` | the operations and the scope |
| `cli` | the command line, the project file, saved sessions |
| `docs` | the manual, the README, the release, the rules of the code base, this layout |
| `equiv` | the equivalence check |
| `perf` | the cost of a load |
| `support` | helpers and designs that the tests share |

## Set up

You need Python 3.11 or newer. CI uses uv. Create a virtual environment and install hdl-vet with the tools for development:

```sh
uv venv
uv pip install -e . --group dev
```

After this, `ruff`, `mypy` and `pytest` are not on your path. A `ruff` of another version can run in their place. Do one of two things. Activate the environment, or put `uv run --no-sync` before each command, as CI does. The blocks below use `uv run --no-sync`.

Run the checks of the first job of CI. Run them before you open a pull request:

```sh
uv run --no-sync ruff check
uv run --no-sync ruff format --check
uv run --no-sync mypy
uv run --no-sync pytest -q tests/docs/test_project_rules.py
```

The file `tests/docs/test_project_rules.py` holds the rules of the code base. The versions of `ruff` and `mypy` are fixed in `pyproject.toml`, so that a new release cannot break an unrelated pull request.

The group `dev` also installs Yosys for the equivalence check of `hdl_vet.equiv`. Users get Yosys from the optional extra `equiv`. Without Yosys, the tests that need it skip. If you set `HDL_VET_REQUIRE_EQUIV=1`, they fail instead, as CI does.

While you work, run the fast tests. They skip the tests that need the corpus:

```sh
uv run --no-sync pytest -q -m "not corpus"
```

The corpus is a set of designs that hdl-bag, a generator, writes. The corpus tests need these things:

- hdl-bag is installed in the same environment, so that the command `bag` exists. For example, run `uv pip install ../hdl-bag`.
- A clone of hdl-bag is the folder `../hdl-bag` beside your clone of hdl-vet. If the clone is in another place, set the variable `HDL_BAG_REPO` to its folder.
- The clone is at the commit that CI uses. The file `.github/workflows/ci.yml` names it. The tests pin numbers that were measured on that commit.

If `bag` or the clone is missing, pytest skips the corpus tests that need it. If you set `HDL_VET_REQUIRE_CORPUS=1`, they fail in this case. CI sets this variable. Then run all tests:

```sh
uv run --no-sync pytest -q
```

Some corpus tests prove a module of a large design. They have the marker `bigproof`. Each one takes about one minute and about 2.5 GB of memory. In CI, one job runs them. To run all tests but these, add `-m "not bigproof"`.

If you change the output of a command, the transcripts of the manual and of the README change. If you change the scripting API, the same command rewrites `docs/manual/api.tex`. Rewrite them with this command. Before you commit, read the diff.

```sh
HDL_VET_MANUAL_UPDATE=1 uv run --no-sync pytest -q tests/docs/test_manual.py tests/docs/test_readme.py
```

This form is for a POSIX shell. In PowerShell, set `$env:HDL_VET_MANUAL_UPDATE = "1"` first. Then run the command without the first word.

To build the PDF of the manual, you need `latexmk` and a TeX distribution. Run this command in the folder `docs/manual`:

```sh
latexmk -pdf -interaction=nonstopmode -halt-on-error hdl-vet.tex
```

## What a pull request must satisfy

Before you open a pull request, make sure that each item is true:

- [ ] Tests state the behaviour of your change. If your change changes behaviour, a test fails without it.
- [ ] No behaviour changes without a test.
- [ ] You did not weaken, skip or delete a test to make code pass.
- [ ] If you think a test is wrong, you say so in the pull request. You do not change the test silently.
- [ ] `ruff check`, `ruff format --check` and `mypy` print no finding.
- [ ] Only `hdl_vet.slang_adapter` imports `pyslang`.
- [ ] No module of the package imports `pickle`, `cPickle`, `marshal`, `shelve` or `dill`. No module calls `eval`, `exec`, `compile` or `__import__`. The one exception is `cli._load_script`, which runs the scripts that the user names.
- [ ] No module of the package imports a helper of the tests.
- [ ] The manual changes with the behaviour. The tests of the manual find a name of the scripting API without an entry. They find an option without an entry. They find an operation without an example. They find a transcript that no longer prints what it shows. They do not find text that is now false, so read the text near your change.
- [ ] Every example in the manual and in the README is a file or a transcript that a test runs.
- [ ] The text that you write follows the rules in the section "Writing".
- [ ] The tests pass on Python 3.11 and on Python 3.13, on Linux, on macOS and on Windows. CI runs them with the corpus.
- [ ] The wheel builds, installs alone and passes `tests/smoke_wheel.py`. The `package` job of CI does this.
- [ ] If you change `docs/manual`, the PDF of the manual builds without an error and without an undefined reference. The `manual` workflow does this.

## A larger change

For a larger change, we recommend this flow of two reviews. It is not a requirement. It is the flow that this project used.

1. Write the specification and the tests. The specification is the docstring of a test file.
2. Review the tests against the real code before you write the implementation.
3. Write the implementation. Do not change the tests.
4. Make an adversarial review of the result. The reviewer tries to break it. The reviewer is a person or a tool other than the one that wrote the code. The reviewer did not see how the code was written.
5. Turn each finding of the review into a test.
6. Fix the code in one more round, with the new tests.

## Work with an AI tool

You can use an AI tool. You do not have to say that you used one. You answer for every line of your pull request as your own work.

- Read every line that the tool wrote. Run it. Be able to explain it. A reviewer can ask you to explain any line.
- Check every statement about behaviour with a run. Do not take it from the tool.
- Do not let the tool edit a test to make it pass.
- Do not give the tool material that you have no right to share.
- Do not submit text that you have no right to license under Apache-2.0.

The file `AGENTS.md` tells an AI agent how work is done in this repository. Give it to your tool.

## Writing

The text of the manual, of the README and of this file follows the writing rules of ASD-STE100, Simplified Technical English. The file `tests/docs/test_manual_style.py` states the rules. Tests check these rules:

- A sentence has 25 words or fewer. A sentence in a numbered list has 20 words or fewer.
- A paragraph has 6 sentences or fewer.
- Some words are not allowed. The test file lists them with a plain replacement for each.
- Prose has no contraction and no semicolon.

A reviewer checks the other rules:

- Use one word for one thing. Use the terms of the glossary in the manual, for example input tree, output tree, script and operation.
- Write in the active voice. Name the one who acts.
- Write an instruction in the imperative, one instruction in a sentence. Put the condition before the instruction.
- Write one idea in a sentence. Write one topic in a paragraph.
- Use the simple tenses: present, past, future and imperative.
- Do not use an "-ing" form as a verb. Write "runs", not "is running".
- Use a noun group of three words or fewer. Write the articles.
- Use a vertical list when the text depends on its introduction.

## Make a release

A maintainer makes a release. A version number can be used only once on PyPI. You cannot replace a file after the upload, and you cannot reuse a number that you delete. Make sure that all is right before step 6.

1. Choose the new version X.Y.Z. Write it in `pyproject.toml`, in `src/hdl_vet/__init__.py` and in `docs/manual/version.tex`.
2. Run `python .github/scripts/check_version.py`. If it prints a line, the three versions differ. Fix them.
3. Open a pull request with this change. Merge it when all checks pass.
4. Start the workflow by hand on the branch `main`. The file is `release.yml`. The page "Actions" lists it as `release`. Select "Run workflow".
5. Look at the result of the trial. If a job fails, fix the cause in a new pull request. Then go back to step 4.
6. Open the project page on TestPyPI and read it. A second trial of the same version uploads nothing. The project page on TestPyPI shows the first upload.
7. Run `git fetch origin`. Then make the tag on the commit of `main` that the trial ran on: `git tag -a vX.Y.Z -m "hdl-vet X.Y.Z" COMMIT`.
8. Push the tag by name: `git push origin vX.Y.Z`. Never push all tags.
9. When the job `pypi` waits, open the run. Select "Review deployments", select `pypi`, then select "Approve and deploy".
10. Wait for the job `release`. Open the new release on GitHub. Make sure that it holds the wheel, the source package and the PDF of the manual.

The tag must be "v" and the version, for example `v0.1.0`. The first job of the workflow fails if it is not. The first job also fails if the tagged commit is not in `main`. The workflow makes the GitHub release only for a pushed tag, never for a run by hand.

If the job `pypi` fails before any file is on PyPI, correct the cause. Then delete the tag and push it again. If any file is on PyPI, do not use the version again. Change the version to the next one and start at step 1.

If only the job `release` fails, select "Re-run failed jobs". Do not select "Re-run all jobs". A full run fails in the job `pypi`, because the files exist.

## Licence

hdl-vet is under the Apache-2.0 licence. The text is in [LICENSE](LICENSE). When you open a pull request, you offer your contribution under this licence. There is no other form to sign.

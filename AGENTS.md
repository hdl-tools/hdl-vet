# Instructions for AI agents

This file tells an AI agent how work is done in this repository. Read it before you change anything. `CONTRIBUTING.md` has the rules for every contributor and the layout of the code. This file adds the way of working that the project uses with agents.

The owner of the repository decides. When you are in doubt, ask. Do not assume.

## Read first

1. `README.md`: what hdl-vet is.
2. `CONTRIBUTING.md`: the set-up, the layout tables, what a pull request must satisfy, the writing rules.
3. The docstring of the test file for the area that you work on. The docstrings are the specification.
4. `plan/implementation-plan.md`, if it exists on this machine. It is not in git. It holds the phase table and a dated log.

## The roles

Work on a part is done by three roles. One agent does not take two roles on the same part.

| Role | Who | What it does |
| --- | --- | --- |
| Planner | The most capable model | Asks the owner the design questions. Writes the specification and the acceptance tests. Owns the tests. Verifies every result with its own run. |
| Reviewer | A strong model, started fresh | Tries to break the implementation. Does not see the planner's opinion of it. Changes nothing in the repository. |
| Executor | A fast model | Implements. The tests are frozen for it. |

So far the planner was Claude Fable, the reviewer Claude Opus and the executor Claude Sonnet.

## The flow of a part

1. The planner asks the owner the design questions, each with options and one recommendation.
2. The planner writes the specification as the docstring of a new test file, and the tests under it. The rules have short names, for example `O1` or `P3`. The tests fail for the missing feature. The planner checks that they fail for that reason.
3. The executor implements. If a test is wrong, the executor reports it with evidence and a corrected assertion. It does not edit the test and does not bend the code to it.
4. The planner runs the full suite, the lint and the type check itself. It does not take the executor's numbers.
5. The reviewer attacks the result. The brief lists what must not happen, in order of importance. The reviewer writes designs and scripts in a scratch folder and runs them.
6. The planner turns each finding into a rule and a failing test in a `*_review.py` file. A finding that is not fixed becomes a stated limit, in the docstring and in the manual.
7. The executor fixes. The planner verifies again. A change to code that decides correctness gets a second, narrow review.
8. The planner asks the owner before it commits, unless the owner gave a standing permission for that part. Then: commit, pull request, watch CI. The owner merges.
9. The planner writes the result into the plan log.

## Rules that always hold

- A wrong "ok" or a wrong "proved" is the worst result. An error is always acceptable when in doubt.
- Only the package `src/hdl_vet/slang_adapter/` imports pyslang. The pyslang version is pinned.
- No `pickle`, `eval` or `exec`, except the one place that loads a script.
- Decide from the syntax tree or the elaborated design through the adapter. Do not decide from a search in the text.
- A refusal message starts with the operation and the module, for example `override_net core: `.
- Every example in the README and in the manual is run by a test. Do not write an example by hand.
- No module has more than 900 lines. `CONTRIBUTING.md` names every module and test folder. A test checks both.
- Do not edit a test to make it pass.
- State what you did not check.

## The machine

- Do not change another repository on the machine. The hdl-bag checkout is read-only for you.
- Do not write into `.corpus/` by hand. The corpus tests build it.
- Do not run `pip install` or `uv pip install` on the host as a sub-agent. Use a named scratch environment or a container. A stray install once overwrote the project environment.
- Write scratch files outside the repository.
- Memory is limited. Run one pytest process at a time. Never run two Yosys processes at once. A proof on the large generated design takes about 2.5 GB.
- Do not put the name, the e-mail address or project details of the owner into a request to an outside service.

## Commands

The set-up is in `CONTRIBUTING.md`. These are the checks that a part must pass:

```sh
uv run --no-sync ruff check
uv run --no-sync ruff format --check
uv run --no-sync mypy
HDL_VET_REQUIRE_EQUIV=1 HDL_VET_REQUIRE_CORPUS=1 uv run --no-sync pytest -q
```

- `-m "not corpus"` leaves out the tests that need hdl-bag. `-m "not bigproof"` leaves out the proofs on the large design.
- The corpus must come from the hdl-bag commit that `.github/workflows/ci.yml` names. Install hdl-bag from that commit, and set `HDL_BAG_REPO` to a clone at that commit.
- The corpus cache in `.corpus/` is keyed on the version text of hdl-bag and on the specification. A newer hdl-bag with the same version text does not rebuild it. Ask the owner, then delete `.corpus/` and let the tests build it again.
- Build the manual in `docs/manual` with `latexmk -g -pdf -interaction=nonstopmode -halt-on-error hdl-vet.tex`. The log must have no warning.

## Traps that cost time before

- A shell here-document destroys backslashes and backticks. Write `.tex` files, and any file with `\n` in a string, with a file tool.
- Git on Windows prints warnings about line ends. `.gitattributes` keeps the examples and the `.tex` files at LF.
- The Yosys runtime on Linux sees the host folders under another name. A path that is clean on Windows can still show in a message on Linux. CI is the check.
- A test that names a signal of a generated design is tied to one hdl-bag commit. Check the name at the pinned commit.
- A command that mixes `git commit` with other tools can be stopped by a hook on the machine. Run the commit by itself.

## Writing

- The manual, the README and `CONTRIBUTING.md` follow the writing rules in `CONTRIBUTING.md`. Tests check some of them. Write this file and the test docstrings in the same plain style.
- A report to the owner starts with the result. It says what was run, what failed, and what is still open. It does not hide a failure.
- A commit message has a subject line and a body that says what changed and why. It has no attribution line for a tool.
- A pull request has the sections Summary, Review (when there was one), Stated limits (when there are some) and Checks.

## Git

- Branch from `main`. Use a name such as `d6/override-net` for a part of a phase, or `ci/...`, `chore/...`, `docs/...`.
- One part is one pull request. A fix that CI asks for goes into the same pull request as a new commit.
- Commit or push only when the owner asked. An approval covers one commit or one pull request, unless the owner said more.
- Do not rewrite history that is pushed. Do not skip hooks.
- A release is made by the workflow, as `CONTRIBUTING.md` says. An agent does not start it.

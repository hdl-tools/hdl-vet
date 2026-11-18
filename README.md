# hdl-vet

hdl-vet applies scripted, repeatable and verified edits to generated SystemVerilog.

## What it does

A generator writes your SystemVerilog. You need a few changes in its output, for example a new port on the top module. If you edit the generated files by hand, the next run of the generator removes your edits.

hdl-vet keeps the edits in a script. A script is a short Python file. When the generator runs again, you run the same script on the new files and you get the same edits.

hdl-vet never changes the input tree. The input tree is the folder that holds the design as the generator wrote it. hdl-vet writes the output tree, which is a copy of the input tree with your edits. Text that no operation edits stays byte-identical in the output tree. hdl-vet adds one comment line to each file that it changes.

Three protections stop a wrong edit:

- An operation refuses a design that it cannot edit safely. The exit status is 2 and hdl-vet writes nothing.
- The argument `was` of an operation says what the design shows now. If the design shows something else, hdl-vet stops the run and writes nothing. The exit status is 2.
- After the last script, hdl-vet checks the result. This check is the sign-off. It loads the edited design with the slang compiler and compares it with the design that you started from. If the sign-off fails, the exit status is 1 and hdl-vet writes no output tree.

The sign-off fails in these cases:

- The design shows a change that no operation declared.
- The compiler reports a new error or warning.
- A signal has no driver now, and something reads it.
- The edited design reads a file that the original design did not read.
- A change that an operation must cause is not in the design.
- A later edit undid the result of an operation.
- The equivalence check does not prove that an override has no effect when it is switched off.
- The enable of an override is not off in the design, and the override has no waiver. A pin is off when it connects a constant 0 or a new input port of the parent.

## Status

This is an early version. These parts work today:

- operations on ports, parameters, wires, assign statements and connections
- operations that act through the hierarchy, such as a new port on every module along a path
- the replacement of the module of an instance, and the removal of instances, ports, parameters, wires and assign statements
- a project file, `hdl-vet.toml`, that names the design, the scripts and the output folder
- saved sessions that you can resume later
- scopes that limit an operation to one part of the design
- one operation that changes logic, `Module.override_net`, which puts a multiplexer on a net, for example a bypass of a reset synchroniser
- an equivalence check in the sign-off, which proves that such a change has no effect when its enable is 0

Only `Module.override_net` changes logic. The sign-off proves the edited module with Yosys. The proof needs the extra `hdl-vet[equiv]`. Without it, the operation is refused unless it has a waiver. A waiver is a text that says why the proof is not needed.

The sign-off does not see these changes:

- a change of logic that you make with a raw edit of the text, which is `Vet.splice`
- a change of logic above the edited module, because the proof is of the edited module only
- a change of the delay or the strength of a gate primitive
- a signal that loses the driver of only some of its bits

The operations refuse these things:

- instances in generate blocks, in instance arrays, or from a macro
- positional connections and `.*` connections
- a module with a port list that is not in the ANSI style
- a port list or a parameter list that holds a conditional compilation directive

The chapter "Limits and known problems" of the manual lists the rest.

## Install

hdl-vet needs Python 3.11 or newer. It has one dependency, `pyslang`, and the installer fetches it. Install hdl-vet from PyPI. Use a virtual environment.

```sh
python -m venv .venv
. .venv/bin/activate
pip install hdl-vet
python -m hdl_vet --version
```

These commands are for a POSIX shell. On Windows, run `.venv\Scripts\activate` in place of the second line. If you use uv, run `uv venv`. Activate the environment as above. Then run `uv pip install hdl-vet`.

The first run below uses the example design of the manual, which is in this repository. To get it, make a clone:

```sh
git clone https://github.com/hdl-tools/hdl-vet.git
cd hdl-vet
```

To install hdl-vet from the clone, run `pip install .` in it.

To use `Module.override_net` with the proof, install the extra. Run `pip install "hdl-vet[equiv]"`, or `pip install ".[equiv]"` in the clone.

## A first run

The example design of the manual has four source files. Copy it to a scratch folder. Work in that folder, so that the clone stays clean.

```sh
cp -r docs/manual/examples /tmp/vet-examples
cd /tmp/vet-examples
```

These commands are for a POSIX shell. In PowerShell, use another folder in place of `/tmp`. Chapter 3 of the manual shows the PowerShell commands.

This script adds the input port `test_en_i` to `soc_top`. It connects the port to the instance `u_rst_sync`, in place of the constant `1'b0`. The argument `was` says what hdl-vet must find there now. If the design shows something else, hdl-vet stops the run and writes nothing. The exit status is 2.

```python
def apply(vet):
    top = vet.module("soc_top")
    top.add_port("test_en_i", "input")
    top.instance("u_rst_sync").connect("test_i", "test_en_i", was="1'b0")
```

Make a project. Then put the script in it. The starter script of the project changes nothing, and the second command replaces it.

```console
$ vet init myproj -f design/design.f --top soc_top
created myproj
wrote hdl-vet.toml
wrote scripts/01_example.py
wrote cells/README.md
wrote README.md
wrote .gitignore
$ cp scripts/first.py myproj/scripts/01_example.py
```

Enter the project. Then run the script with `--dry-run`. The command writes no output tree. Look at the diff and at the last line, which counts the operations.

```console
$ cd myproj
$ vet apply --dry-run
project: <examples>/myproj/hdl-vet.toml
--- a/rtl/soc_top.sv
+++ b/rtl/soc_top.sv
@@ -1,4 +1,5 @@
 // Generated by a generator. Do not edit.
+// Modified by hdl-vet; scripts: 01_example.py sha256:1ed8008abb9a

 module soc_top (
   input  logic       clk_i,
@@ -6,7 +7,8 @@
   input  logic [1:0] en_i,
   output logic [7:0] count0_o,
   output logic [7:0] count1_o,
-  output logic       irq_o
+  output logic       irq_o,
+  input  logic test_en_i
 );
   logic       rst_sync_n;
   logic [1:0] irq;
@@ -14,7 +16,7 @@
   rst_sync #(.STAGES(2)) u_rst_sync (
     .clk_i  (clk_i),
     .rst_ni (rst_ni),
-    .test_i (1'b0),
+    .test_i (test_en_i),
     .rst_no (rst_sync_n)
   );

2 applied, 0 skipped
```

Run the same command without `--dry-run`. Look at the line `changed rtl/soc_top.sv`. It is the only file that changed. The other files of the input tree are copies in the output tree.

```console
$ vet apply
project: <examples>/myproj/hdl-vet.toml
wrote <examples>/myproj/out
2 applied, 0 skipped
changed rtl/soc_top.sv
```

## Documentation

The user manual is in `docs/manual`. It is written in LaTeX. The chapters are in [docs/manual/chapters](docs/manual/chapters). A test runs every example of the manual.

To get the PDF, download the artifact `hdl-vet-manual` from a run of the `manual` workflow. To build it on your machine, run `latexmk -pdf hdl-vet.tex` in the folder `docs/manual`.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. It tells you how to set up, what a pull request must satisfy and how to write.

## Licence

hdl-vet is free software under the Apache-2.0 licence. The text is in [LICENSE](LICENSE).

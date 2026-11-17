"""The cells that test_instances.py and the save and resume tests use."""

from __future__ import annotations

# Cells a script may add. Each is a drop-in for `leaf` unless its name says otherwise.
CELLS = {
    "cell_leaf.sv": (
        "module cell_leaf (\n"
        "  input  logic       CK,\n"
        "  input  logic [3:0] D,\n"
        "  output logic [3:0] Q\n"
        ");\n"
        "  always_ff @(posedge CK) Q <= D;\n"
        "endmodule\n"
    ),
    "wide_leaf.sv": (
        "module wide_leaf (\n"
        "  input  logic       clk,\n"
        "  input  logic [7:0] d,\n"
        "  output logic [3:0] q\n"
        ");\n"
        "  always_ff @(posedge clk) q <= d[3:0];\n"
        "endmodule\n"
    ),
    "backwards_leaf.sv": (
        "module backwards_leaf (\n"
        "  input  logic       clk,\n"
        "  output logic [3:0] d,\n"
        "  output logic [3:0] q\n"
        ");\n"
        "  assign d = '0;\n"
        "  assign q = '0;\n"
        "endmodule\n"
    ),
    "deep_leaf.sv": (
        "module deep_leaf #(\n"
        "  parameter int WIDTH = 4,\n"
        "  parameter int DEPTH = 5\n"
        ") (\n"
        "  input  logic             clk,\n"
        "  input  logic [WIDTH-1:0] d,\n"
        "  output logic [WIDTH-1:0] q\n"
        ");\n"
        "  always_ff @(posedge clk) q <= d;\n"
        "endmodule\n"
    ),
    "scan_leaf.sv": (
        "module scan_leaf (\n"
        "  input  logic       clk,\n"
        "  input  logic [3:0] d,\n"
        "  input  logic       scan_en,\n"
        "  output logic [3:0] q,\n"
        "  output logic       ready\n"
        ");\n"
        "  always_ff @(posedge clk) if (!scan_en) q <= d;\n"
        "  assign ready = 1'b1;\n"
        "endmodule\n"
    ),
    "second_leaf.sv": "module leaf (input logic clk);\nendmodule\n",
    "broken.sv": "module broken (input logic clk;\nendmodule\n",
    "includer.sv": '`include "cell_defs.svh"\nmodule includer (input logic clk);\nendmodule\n',
    "cell_defs.svh": "`define CELL_W 4\n",
    "warns.sv": (
        "module warns (\n"
        "  input  logic       clk,\n"
        "  input  logic [3:0] d,\n"
        "  output logic [3:0] q\n"
        ");\n"
        "  logic [1:0] narrow;\n"
        "  assign narrow = d;\n"
        "  always_ff @(posedge clk) q <= {2'b0, narrow};\n"
        "endmodule\n"
    ),
}

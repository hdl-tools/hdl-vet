`include "macros.svh"

module top (
  input  logic       clk_i,
  input  logic       sel_i,
  input  logic [7:0] d_i,
  output logic [7:0] q_o,
  output wire        w_o
);
  logic [7:0] mid_q;
  logic [7:0] macro_q;

  mid u_mid (
    .clk_i (clk_i),
    .d_i   (d_i),
    .q_o   (mid_q)
  );

  `LEAF_INST(u_macro_leaf, mid_q, macro_q)

  assign q_o = sel_i ? macro_q : mid_q;

  // Two continuous drivers on one wire: legal, and what drivers_of must count.
  assign w_o = sel_i;
  assign w_o = clk_i;
endmodule

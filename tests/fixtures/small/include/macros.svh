`ifndef SMALL_MACROS_SVH
`define SMALL_MACROS_SVH
`define LEAF_INST(name, d, q) \
  leaf #(.W(8)) name (.clk_i(clk_i), .d_i(d), .q_o(q));
`endif

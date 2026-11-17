// A registered leaf.
module leaf #(
  parameter int unsigned W = 8
) (
  input  logic         clk_i,
  input  logic [W-1:0] d_i,
  output logic [W-1:0] q_o
);
  always_ff @(posedge clk_i) q_o <= d_i;
endmodule

module mid (
  input  logic                         clk_i,
  input  logic [small_pkg::Width-1:0]  d_i,
  output logic [small_pkg::Width-1:0]  q_o
);
  logic [small_pkg::Width-1:0] stage;

  leaf #(
    .W (small_pkg::Width)
  ) u_leaf_a (
    .clk_i (clk_i),
    .d_i   (d_i),
    .q_o   (stage)
  );

  leaf #(
    .W (small_pkg::Width)
  ) u_leaf_b (
    .clk_i (clk_i),
    .d_i   (stage),
    .q_o   (q_o)
  );
endmodule

// Reset synchroniser: asynchronous assert, synchronous release.
// With test_i high the reset passes straight through.
module rst_sync #(
  parameter int STAGES = 2
) (
  input  logic clk_i,
  input  logic rst_ni,
  input  logic test_i,
  output logic rst_no
);
  logic [STAGES-1:0] q;

  always_ff @(posedge clk_i or negedge rst_ni)
    if (!rst_ni) q <= '0;
    else         q <= {q[STAGES-2:0], 1'b1};

  assign rst_no = test_i ? rst_ni : q[STAGES-1];
endmodule

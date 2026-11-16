// Free-running counter with an interrupt when it is all ones.
module timer #(
  parameter int WIDTH = 8
) (
  input  logic             clk_i,
  input  logic             rst_ni,
  input  logic             en_i,
  output logic [WIDTH-1:0] count_o,
  output logic             irq_o
);
  always_ff @(posedge clk_i or negedge rst_ni)
    if (!rst_ni)   count_o <= '0;
    else if (en_i) count_o <= count_o + 1'b1;

  assign irq_o = &count_o;
endmodule

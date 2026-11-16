// Technology reset synchroniser with scan pins. Not part of the generated
// design: a script brings it in with vet.add_file.
module tech_rst_sync #(
  parameter int DEPTH = 2
) (
  input  logic CK,
  input  logic RN,
  input  logic SE,
  output logic Q,
  output logic SO
);
  logic [DEPTH-1:0] q;

  always_ff @(posedge CK or negedge RN)
    if (!RN) q <= '0;
    else     q <= {q[DEPTH-2:0], 1'b1};

  assign Q  = SE ? RN : q[DEPTH-1];
  assign SO = q[DEPTH-1];
endmodule

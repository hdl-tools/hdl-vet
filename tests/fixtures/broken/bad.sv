module bad (
  input  logic a_i,
  output logic y_o
);
  assign y_o = a_i & not_declared;
endmodule

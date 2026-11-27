// Technology isolation cell. Not part of the generated design: a script
// brings it in with vet.add_file. With ISO at 0 the cell passes A to Z.
module tech_iso (
  input  logic A,
  input  logic ISO,
  output logic Z
);
  assign Z = ISO ? 1'b0 : A;
endmodule

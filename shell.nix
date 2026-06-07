{ pkgs ? import <nixpkgs> { } }:

let
  runtimeLibs = with pkgs; [
    stdenv.cc.cc.lib
  ];
in

pkgs.mkShell {
  packages = with pkgs; [
    git
    pandoc
    python313
    uv
    python313Packages.weasyprint
  ];

  LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath runtimeLibs;

  shellHook = ''
    echo "Anthropic Readings development shell"
    echo "Run: uv venv && uv pip install -e ."
    echo "Then: python -m anthropic_readings --check --config config.yaml"
  '';
}

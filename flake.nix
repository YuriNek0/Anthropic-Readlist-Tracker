{
  description = "Nix flake for anthropic-readings-daemon";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { self, nixpkgs, flake-utils }:
    let
      mkUserServiceModule =
        defaultPackage:
        { lib, config, pkgs, ... }:
        let
          cfg = config.services.anthropic-readings;
        in
        {
          options.services.anthropic-readings = {
            enable = lib.mkEnableOption "the Anthropic Readings user daemon";

            package = lib.mkOption {
              type = lib.types.package;
              default = defaultPackage pkgs.system;
              description = "Package providing the anthropic-readings-daemon executable.";
            };

            configFile = lib.mkOption {
              type = lib.types.str;
              example = "/home/alice/.config/anthropic-readings/config.yaml";
              description = "Path to the daemon YAML configuration file.";
            };

            extraArgs = lib.mkOption {
              type = with lib.types; listOf str;
              default = [ ];
              example = [ "--check" ];
              description = "Additional CLI arguments passed to anthropic-readings-daemon.";
            };

            restartDelay = lib.mkOption {
              type = lib.types.str;
              default = "4h";
              example = "6h";
              description = "How long systemd waits before attempting an automatic restart.";
            };
          };

          config = lib.mkIf cfg.enable {
            systemd.user.services.anthropic-readings = {
              Unit = {
                Description = "Anthropic Readings Daemon";
                After = [ "network-online.target" ];
                Wants = [ "network-online.target" ];
              };

              Service = {
                Type = "simple";
                ExecStart = lib.escapeShellArgs (
                  [
                    (lib.getExe cfg.package)
                    "--config"
                    (toString cfg.configFile)
                  ]
                  ++ cfg.extraArgs
                );
                Restart = "always";
                RestartSec = cfg.restartDelay;
                WorkingDirectory = "%h";
              };

              Install = {
                WantedBy = [ "default.target" ];
              };
            };
          };
        };
    in
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        lib = pkgs.lib;
        python = pkgs.python312;
        pythonPackages = python.pkgs;

        anthropic-readings-daemon = pythonPackages.buildPythonApplication {
          pname = "anthropic-readings-daemon";
          version = "0.1.0";
          pyproject = true;
          src = self;

          build-system = with pythonPackages; [
            setuptools
            wheel
          ];

          dependencies = with pythonPackages; [
            azure-core
            msal
            msgraph-sdk
            nbconvert
            python-slugify
            pyyaml
            schedule
          ];

          nativeBuildInputs = [ pkgs.makeWrapper ];

          postInstall = ''
            wrapProgram "$out/bin/anthropic-readings-daemon" \
              --prefix PATH : "${lib.makeBinPath [
                pkgs.git
                pkgs.pandoc
                pythonPackages.weasyprint
              ]}" \
              --set CHROMIUM_PATH "${lib.getExe pkgs.chromium}"
          '';

          pythonImportsCheck = [ "anthropic_readings" ];

          meta = with lib; {
            description = "Daemon that fetches, renders, uploads, and emails Anthropic readings";
            mainProgram = "anthropic-readings-daemon";
            license = licenses.mit;
            platforms = platforms.linux ++ platforms.darwin;
          };
        };
      in
      {
        packages = {
          default = anthropic-readings-daemon;
          inherit anthropic-readings-daemon;
        };

        apps.default = flake-utils.lib.mkApp {
          drv = anthropic-readings-daemon;
        };
      }
    )
    // {
      homeManagerModules.default = mkUserServiceModule (
        system: self.packages.${system}.default
      );

      nixosModules.default = mkUserServiceModule (
        system: self.packages.${system}.default
      );
    };
}

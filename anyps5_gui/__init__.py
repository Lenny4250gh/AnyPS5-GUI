"""AnyPS5 GUI: the code of the app, one module per topic.

Each module only imports from the ones listed before it (no circular imports):

  settings        paths, constants, default settings
  system_checks   CPU, GPU, memory and driver checks
  config          config.ini
  util            small shared helpers
  repo            where the tools come from (release / source paths), relinker options in use
  input_ini       anyps5-input.ini validation
  process         running external programs
  unwrapper       the external unwrap_self.py
  archives        game given as an archive
  files           game files, libs, copy and move
  disk            disk space and desktop notification
  release         GitHub release download
  source_build    relinker built from source, build tools
  env_catalog     catalog of environment variables
  runtime         runtime options, input mapper, shortcut
  launch          starting a converted game
  relink_options  relinker options and checks
  basic_ops       unwrap, restore, trash, data folder
  relink          the conversion (dry run, relink, audit)
  report          exit codes, run report, bug report
  library         converted games found in the output folder and the folders added by the user
  updater         GUI update
  discord_rpc     Discord Rich Presence
  schema          settings schema for the interface
  api             the bridge used by index.html

The program entry point is AnyPS5_GUI.py, next to this folder.
"""

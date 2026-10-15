// Prevents an extra console window on Windows in release, DO NOT REMOVE!!
//
// The cost of this is real and handled in lib.rs: with no console attached,
// stdout goes nowhere, so a fatal startup error would be completely silent --
// the app simply never appears.  `report_fatal()` therefore also writes the
// reason to the conductor log and raises a message box.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    chrono_handoff_lib::run()
}

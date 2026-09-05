mod config;
mod fs_dispatch;
mod fsroots;
mod gateway;
mod pty;
mod pty_output;
mod window;

use tauri::menu::{MenuBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::Manager;

/// Relaunch the app: exec the binary at the same bundle path, which after an
/// in-place update (`install-desktop.sh --in-place`) is the freshly installed
/// build. `request_restart` rather than `restart`: commands run off the main
/// thread, and this variant just flags the restart and asks the event loop to
/// exit, so the IPC response can complete and cleanup runs normally.
#[tauri::command]
fn app_relaunch(app: tauri::AppHandle) {
    app.request_restart();
}

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let app_config = config::load();
            let roots = fsroots::Roots::from_config(&app_config);

            app.manage(app_config);
            app.manage(roots);
            app.manage(fsroots::WatchState::default());
            app.manage(pty::Ptys::default());
            app.manage(gateway::GatewayState::default());

            #[cfg(target_os = "macos")]
            {
                let about = PredefinedMenuItem::about(app, None, None)?;
                let quit = PredefinedMenuItem::quit(app, None)?;
                let app_submenu = SubmenuBuilder::new(app, "turing")
                    .item(&about)
                    .separator()
                    .item(&quit)
                    .build()?;

                let undo = PredefinedMenuItem::undo(app, None)?;
                let redo = PredefinedMenuItem::redo(app, None)?;
                let cut = PredefinedMenuItem::cut(app, None)?;
                let copy = PredefinedMenuItem::copy(app, None)?;
                let paste = PredefinedMenuItem::paste(app, None)?;
                let select_all = PredefinedMenuItem::select_all(app, None)?;
                let edit_submenu = SubmenuBuilder::new(app, "Edit")
                    .item(&undo)
                    .item(&redo)
                    .separator()
                    .item(&cut)
                    .item(&copy)
                    .item(&paste)
                    .item(&select_all)
                    .build()?;

                let menu = MenuBuilder::new(app)
                    .item(&app_submenu)
                    .item(&edit_submenu)
                    .build()?;
                app.set_menu(menu)?;
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            config::app_config,
            fsroots::fs_list,
            fsroots::fs_read_text,
            fsroots::fs_read_binary,
            fsroots::fs_tail,
            fsroots::fs_open_external,
            fsroots::fs_watch,
            pty::pty_spawn,
            pty::pty_write,
            pty::pty_resize,
            pty::pty_kill,
            gateway::gateway_fetch,
            gateway::gateway_ws_start,
            window::warp_cursor,
            app_relaunch,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

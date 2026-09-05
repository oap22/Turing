// Desktop-shell config: gateway base/token + filesystem roots the frontend is
// allowed to touch via `fsroots.rs`. Read-only from the frontend's point of
// view — `app_config()` never returns the token, only whether one is set.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

#[derive(Clone, Serialize, Deserialize)]
pub struct RootCfg {
    pub id: String,
    pub path: String,
}

#[derive(Clone, Serialize, Deserialize)]
pub struct AppConfig {
    pub gateway_base: String,
    pub gateway_token: String,
    pub roots: Vec<RootCfg>,
}

// The on-disk shape is a partial overlay: any field may be omitted, in which
// case the default is kept. `roots`, when present, replaces the default list
// wholesale (it does not merge).
#[derive(Deserialize)]
struct UserConfig {
    gateway_base: Option<String>,
    gateway_token: Option<String>,
    roots: Option<Vec<RootCfg>>,
}

fn expand_home(path: &str) -> String {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = dirs::home_dir() {
            return home.join(rest).to_string_lossy().into_owned();
        }
    } else if path == "~" {
        if let Some(home) = dirs::home_dir() {
            return home.to_string_lossy().into_owned();
        }
    }
    path.to_string()
}

fn default_roots() -> Vec<RootCfg> {
    vec![
        RootCfg {
            id: "vault".to_string(),
            path: expand_home("~/Owen's Awesome Vault"),
        },
        // Project-agnostic on purpose: the watched results root lives outside
        // any one repo, so a run in ~/Developer/active/mnist (or anywhere
        // else) lights up the metrics/images panes without dumping artifacts
        // into the Turing checkout. The id stays "results" — the frontend
        // panes hardcode it.
        RootCfg {
            id: "results".to_string(),
            path: expand_home("~/research-results"),
        },
        RootCfg {
            id: "repo".to_string(),
            path: expand_home("~/Developer/active/Turing"),
        },
        RootCfg {
            id: "claude-sessions".to_string(),
            path: expand_home("~/.claude/projects"),
        },
        RootCfg {
            id: "codex-sessions".to_string(),
            path: expand_home("~/.codex/sessions"),
        },
    ]
}

pub fn default_config() -> AppConfig {
    AppConfig {
        gateway_base: "http://127.0.0.1:8765".to_string(),
        gateway_token: String::new(),
        roots: default_roots(),
    }
}

// On Windows the overlay lives under %APPDATA% (FOLDERID_RoamingAppData),
// the per-user roaming-config convention — `dirs::config_dir()` resolves it.
#[cfg(windows)]
fn config_path() -> Option<PathBuf> {
    dirs::config_dir().map(|c| c.join("turing-desktop").join("config.json"))
}

// Resolve the base config directory per platform.
//
// macOS deliberately stays `~/.config` (NOT `~/Library/Application Support`,
// which is what `dirs::config_dir()` would return): the path is documented,
// scripted against, and part of the existing install's contract.
#[cfg(target_os = "macos")]
fn config_dir() -> Option<PathBuf> {
    dirs::home_dir().map(|h| h.join(".config"))
}

// Linux (and other unix) honors the XDG Base Directory spec:
// `$XDG_CONFIG_HOME` when set to an absolute path, else `~/.config`.
#[cfg(all(not(windows), not(target_os = "macos")))]
fn config_dir() -> Option<PathBuf> {
    xdg_config_dir(std::env::var_os("XDG_CONFIG_HOME"), dirs::home_dir())
}

// Pure XDG resolution, split out so it is unit-testable on any host. The spec
// says an empty or relative `$XDG_CONFIG_HOME` "should be ignored", so both
// fall back to `~/.config`.
#[cfg_attr(any(target_os = "macos", windows), allow(dead_code))]
fn xdg_config_dir(xdg: Option<std::ffi::OsString>, home: Option<PathBuf>) -> Option<PathBuf> {
    if let Some(dir) = xdg {
        let dir = PathBuf::from(dir);
        if dir.is_absolute() {
            return Some(dir);
        }
    }
    home.map(|h| h.join(".config"))
}

// Everywhere outside Windows, the overlay remains under turing-desktop.
// macOS preserves its literal ~/.config contract; Linux and other Unix hosts
// resolve the base with the XDG rule above.
#[cfg(not(windows))]
fn config_path() -> Option<PathBuf> {
    config_dir().map(|d| d.join("turing-desktop/config.json"))
}

pub fn load() -> AppConfig {
    let defaults = default_config();
    let Some(path) = config_path() else {
        return defaults;
    };
    let Ok(text) = fs::read_to_string(&path) else {
        return defaults;
    };
    let Ok(user) = serde_json::from_str::<UserConfig>(&text) else {
        return defaults;
    };
    AppConfig {
        gateway_base: user.gateway_base.unwrap_or(defaults.gateway_base),
        gateway_token: user.gateway_token.unwrap_or(defaults.gateway_token),
        roots: user.roots.unwrap_or(defaults.roots),
    }
}

#[derive(Serialize)]
pub struct AppConfigView {
    pub gateway_base: String,
    pub gateway_token_set: bool,
    pub roots: Vec<RootCfg>,
}

#[tauri::command]
pub fn app_config(state: tauri::State<'_, AppConfig>) -> AppConfigView {
    AppConfigView {
        gateway_base: state.gateway_base.clone(),
        gateway_token_set: !state.gateway_token.is_empty(),
        roots: state.roots.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn defaults_on_missing_file() {
        let cfg = default_config();
        assert_eq!(cfg.gateway_base, "http://127.0.0.1:8765");
        assert_eq!(cfg.gateway_token, "");
        assert_eq!(cfg.roots.len(), 5);
        assert_eq!(cfg.roots[0].id, "vault");
    }

    // The results root is deliberately repo-independent, and its id is part of
    // the frontend contract (MetricsPane/ImagesPane/FlywheelPane look up
    // "results" by name).
    #[test]
    fn results_root_is_project_agnostic() {
        let cfg = default_config();
        let results = cfg
            .roots
            .iter()
            .find(|r| r.id == "results")
            .expect("results root");
        assert_eq!(results.path, expand_home("~/research-results"));
        assert!(!results.path.contains("Turing"));
        assert!(!results.path.starts_with('~'));
    }

    #[test]
    fn parses_full_config_from_temp_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        let mut f = fs::File::create(&path).unwrap();
        write!(
            f,
            r#"{{"gateway_base":"http://example:1234","gateway_token":"secret","roots":[{{"id":"a","path":"/tmp/a"}}]}}"#
        )
        .unwrap();
        let text = fs::read_to_string(&path).unwrap();
        let user: UserConfig = serde_json::from_str(&text).unwrap();
        let defaults = default_config();
        let cfg = AppConfig {
            gateway_base: user.gateway_base.unwrap_or(defaults.gateway_base),
            gateway_token: user.gateway_token.unwrap_or(defaults.gateway_token),
            roots: user.roots.unwrap_or(defaults.roots),
        };
        assert_eq!(cfg.gateway_base, "http://example:1234");
        assert_eq!(cfg.gateway_token, "secret");
        assert_eq!(cfg.roots.len(), 1);
        assert_eq!(cfg.roots[0].id, "a");
    }

    // The config overlay's location is a per-platform contract: macOS keeps
    // the literal ~/.config path, Linux may honor XDG_CONFIG_HOME, and Windows
    // uses %APPDATA%\turing-desktop.
    #[test]
    fn config_path_follows_platform_convention() {
        let path = config_path().expect("config path resolves");
        assert!(path.ends_with("turing-desktop/config.json"));
        #[cfg(target_os = "macos")]
        {
            let home = dirs::home_dir().expect("home dir");
            assert_eq!(path, home.join(".config/turing-desktop/config.json"));
        }
        #[cfg(windows)]
        {
            let normalized = path.to_string_lossy().replace('\\', "/");
            let cfg_dir = dirs::config_dir().expect("config dir");
            assert_eq!(path, cfg_dir.join("turing-desktop").join("config.json"));
            // dirs::config_dir() on Windows is Roaming AppData, not ~/.config.
            assert!(!normalized.contains("/.config/"));
        }
    }

    #[test]
    fn xdg_config_dir_prefers_absolute_xdg_config_home() {
        let got = xdg_config_dir(
            Some(std::ffi::OsString::from("/custom/xdg")),
            Some(PathBuf::from("/home/o")),
        );
        assert_eq!(got, Some(PathBuf::from("/custom/xdg")));
    }

    #[test]
    fn xdg_config_dir_ignores_empty_and_relative_values() {
        // Per the XDG Base Directory spec, empty or relative values are
        // ignored and the ~/.config default applies.
        let home = Some(PathBuf::from("/home/o"));
        for bad in ["", "relative/path"] {
            let got = xdg_config_dir(Some(std::ffi::OsString::from(bad)), home.clone());
            assert_eq!(got, Some(PathBuf::from("/home/o/.config")));
        }
    }

    #[test]
    fn xdg_config_dir_falls_back_to_home_dot_config() {
        let got = xdg_config_dir(None, Some(PathBuf::from("/home/o")));
        assert_eq!(got, Some(PathBuf::from("/home/o/.config")));
        assert_eq!(xdg_config_dir(None, None), None);
    }

    // The full path always ends the same way on every platform; on macOS the
    // base is literally ~/.config regardless of any XDG variable.
    #[test]
    fn config_path_is_under_turing_desktop() {
        let path = config_path().expect("home dir in test env");
        assert!(path.ends_with("turing-desktop/config.json"));
        #[cfg(target_os = "macos")]
        {
            let home = dirs::home_dir().unwrap();
            assert_eq!(path, home.join(".config/turing-desktop/config.json"));
        }
    }

    // Regression for the actual Linux wiring, not just the pure helper: the
    // non-macOS config_dir() must READ $XDG_CONFIG_HOME — the xdg_config_dir
    // tests above and the suffix assertion below would all stay green if the
    // env::var_os line were deleted. Runs in the desktop-linux CI job; not
    // compiled on macOS, where config_dir() is pinned to ~/.config.
    #[cfg(not(target_os = "macos"))]
    #[test]
    fn config_path_honors_custom_absolute_xdg_config_home() {
        // Env mutation is safe here: this is the only test touching
        // XDG_CONFIG_HOME, and the concurrent config_path test asserts a
        // suffix that holds whichever value is in effect.
        std::env::set_var("XDG_CONFIG_HOME", "/custom/xdg");
        let path = config_path().expect("resolvable config path");
        std::env::remove_var("XDG_CONFIG_HOME");
        assert_eq!(
            path,
            PathBuf::from("/custom/xdg/turing-desktop/config.json")
        );
    }

    #[test]
    fn token_redaction_shape() {
        let cfg = AppConfig {
            gateway_base: "http://x".to_string(),
            gateway_token: "hunter2".to_string(),
            roots: vec![],
        };
        let view = AppConfigView {
            gateway_base: cfg.gateway_base.clone(),
            gateway_token_set: !cfg.gateway_token.is_empty(),
            roots: cfg.roots.clone(),
        };
        let json = serde_json::to_string(&view).unwrap();
        assert!(!json.contains("hunter2"));
        assert!(json.contains("gateway_token_set"));
        assert!(json.contains("true"));
    }
}

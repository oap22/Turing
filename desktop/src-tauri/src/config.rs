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
        RootCfg {
            id: "results".to_string(),
            path: expand_home("~/Developer/active/Turing/research/results"),
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

fn config_path() -> Option<PathBuf> {
    dirs::home_dir().map(|h| h.join(".config/turing-desktop/config.json"))
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

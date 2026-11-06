//! The HKCU Run value and other small per-user registry reads.

use windows::core::PCWSTR;
use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS, WIN32_ERROR};
use windows::Win32::System::Registry::{
    RegCloseKey, RegCreateKeyExW, RegDeleteValueW, RegGetValueW, RegOpenKeyExW, RegQueryValueExW,
    RegSetValueExW, HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, KEY_QUERY_VALUE, KEY_SET_VALUE,
    REG_EXPAND_SZ, REG_OPTION_NON_VOLATILE, REG_SZ, REG_VALUE_TYPE, RRF_RT_REG_DWORD,
    RRF_RT_REG_SZ,
};

use crate::pyish::wide;
use crate::startup_rules::{
    command_matches, format_command, validate_length, RegistrationState, RegistrationStatus,
};

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
const DEFAULT_VALUE_NAME: &str = "QuickAccessLauncher";

fn error_text(code: WIN32_ERROR) -> String {
    windows::core::Error::from(code.to_hresult()).message().to_string()
}

fn environment_lookup(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

/// Read the Run value: `Ok(None)` when absent, with its value type otherwise.
fn read_run_value() -> Result<Option<(String, REG_VALUE_TYPE)>, String> {
    let key_name = wide(RUN_KEY);
    let value_name = wide(DEFAULT_VALUE_NAME);
    // SAFETY: strings are NUL-terminated, `key` is closed below, and the data
    // buffer is sized from the first query.
    unsafe {
        let mut key = HKEY::default();
        let opened = RegOpenKeyExW(
            HKEY_CURRENT_USER,
            PCWSTR(key_name.as_ptr()),
            None,
            KEY_QUERY_VALUE,
            &mut key,
        );
        if opened == ERROR_FILE_NOT_FOUND {
            return Ok(None);
        }
        if opened != ERROR_SUCCESS {
            return Err(error_text(opened));
        }
        let mut value_type = REG_VALUE_TYPE::default();
        let mut size = 0_u32;
        let probe = RegQueryValueExW(
            key,
            PCWSTR(value_name.as_ptr()),
            None,
            Some(&mut value_type),
            None,
            Some(&mut size),
        );
        let result = if probe == ERROR_FILE_NOT_FOUND {
            Ok(None)
        } else if probe != ERROR_SUCCESS {
            Err(error_text(probe))
        } else {
            let mut data = vec![0_u8; size as usize + 2];
            let mut read = size;
            let queried = RegQueryValueExW(
                key,
                PCWSTR(value_name.as_ptr()),
                None,
                Some(&mut value_type),
                Some(data.as_mut_ptr()),
                Some(&mut read),
            );
            if queried != ERROR_SUCCESS {
                Err(error_text(queried))
            } else {
                let units: Vec<u16> = data[..read as usize]
                    .chunks_exact(2)
                    .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
                    .take_while(|unit| *unit != 0)
                    .collect();
                Ok(Some((String::from_utf16_lossy(&units), value_type)))
            }
        };
        let _ = RegCloseKey(key);
        result
    }
}

/// Owns only this application's value under the current user's Run key.
#[derive(Default)]
pub struct StartupManager;

impl StartupManager {
    /// Inspect actual HKCU state without mutating it or raising read errors.
    pub fn inspect(&self, desired: bool, executable: &str, arguments: &[&str]) -> RegistrationStatus {
        let expected = format_command(executable, arguments);
        let status = |state, actual: Option<String>, error: Option<String>| RegistrationStatus {
            state,
            desired_enabled: desired,
            expected_command: expected.clone(),
            actual_command: actual,
            error,
        };
        if let Err(error) = validate_length(&expected) {
            // Disabling must remain possible even from a too-long path, while
            // enabling is not a state Windows can honor.
            if desired {
                return status(RegistrationState::Unreadable, None, Some(error.to_string()));
            }
        }
        match read_run_value() {
            Ok(None) => status(RegistrationState::Absent, None, None),
            Err(error) => status(RegistrationState::Unreadable, None, Some(error)),
            Ok(Some((actual, value_type))) => {
                let matches = (value_type == REG_SZ || value_type == REG_EXPAND_SZ)
                    && command_matches(&actual, executable, arguments, &environment_lookup);
                let state = if matches {
                    RegistrationState::Correct
                } else {
                    RegistrationState::Stale
                };
                status(state, Some(actual), None)
            }
        }
    }

    /// Repair registry drift and return a post-write authoritative snapshot.
    pub fn reconcile(
        &self,
        desired: bool,
        executable: &str,
        arguments: &[&str],
    ) -> Result<RegistrationStatus, String> {
        let current = self.inspect(desired, executable, arguments);
        if current.in_sync() {
            return Ok(current);
        }
        if desired {
            self.enable(executable, arguments)?;
        } else {
            self.disable()?;
        }
        Ok(self.inspect(desired, executable, arguments))
    }

    fn enable(&self, executable: &str, arguments: &[&str]) -> Result<(), String> {
        let command = format_command(executable, arguments);
        validate_length(&command).map_err(|error| error.to_string())?;
        let key_name = wide(RUN_KEY);
        let value_name = wide(DEFAULT_VALUE_NAME);
        let data = wide(&command);
        // SAFETY: strings are NUL-terminated, `bytes` views the live UTF-16
        // buffer, and the opened key is closed below.
        unsafe {
            let mut key = HKEY::default();
            let opened = RegCreateKeyExW(
                HKEY_CURRENT_USER,
                PCWSTR(key_name.as_ptr()),
                None,
                PCWSTR::null(),
                REG_OPTION_NON_VOLATILE,
                KEY_SET_VALUE,
                None,
                &mut key,
                None,
            );
            if opened != ERROR_SUCCESS {
                return Err(error_text(opened));
            }
            let bytes = std::slice::from_raw_parts(data.as_ptr().cast::<u8>(), data.len() * 2);
            let written = RegSetValueExW(key, PCWSTR(value_name.as_ptr()), None, REG_SZ, Some(bytes));
            let _ = RegCloseKey(key);
            if written != ERROR_SUCCESS {
                return Err(error_text(written));
            }
        }
        Ok(())
    }

    fn disable(&self) -> Result<(), String> {
        let key_name = wide(RUN_KEY);
        let value_name = wide(DEFAULT_VALUE_NAME);
        // SAFETY: strings are NUL-terminated and the key is closed below.
        unsafe {
            let mut key = HKEY::default();
            let opened = RegOpenKeyExW(
                HKEY_CURRENT_USER,
                PCWSTR(key_name.as_ptr()),
                None,
                KEY_SET_VALUE,
                &mut key,
            );
            if opened == ERROR_FILE_NOT_FOUND {
                return Ok(());
            }
            if opened != ERROR_SUCCESS {
                return Err(error_text(opened));
            }
            let deleted = RegDeleteValueW(key, PCWSTR(value_name.as_ptr()));
            let _ = RegCloseKey(key);
            if deleted != ERROR_SUCCESS && deleted != ERROR_FILE_NOT_FOUND {
                return Err(error_text(deleted));
            }
        }
        Ok(())
    }
}

/// Whether Windows apps currently use the dark theme.
pub fn system_prefers_dark() -> bool {
    let key = wide(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
    let value = wide("AppsUseLightTheme");
    let mut data = 1_u32;
    let mut size = 4_u32;
    // SAFETY: the DWORD buffer and size match RRF_RT_REG_DWORD.
    let status = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            PCWSTR(key.as_ptr()),
            PCWSTR(value.as_ptr()),
            RRF_RT_REG_DWORD,
            None,
            Some(&mut data as *mut u32 as *mut _),
            Some(&mut size),
        )
    };
    status == ERROR_SUCCESS && data == 0
}

/// Operating system text for diagnostics, without machine or user names.
pub fn os_description() -> String {
    let key = wide(r"Software\Microsoft\Windows NT\CurrentVersion");
    let read = |name: &str| -> Option<String> {
        let value = wide(name);
        let mut buffer = [0_u16; 128];
        let mut size = (buffer.len() * 2) as u32;
        // SAFETY: the buffer and byte size describe valid writable memory.
        let status = unsafe {
            RegGetValueW(
                HKEY_LOCAL_MACHINE,
                PCWSTR(key.as_ptr()),
                PCWSTR(value.as_ptr()),
                RRF_RT_REG_SZ,
                None,
                Some(buffer.as_mut_ptr().cast()),
                Some(&mut size),
            )
        };
        (status == ERROR_SUCCESS).then(|| {
            let length = buffer.iter().position(|unit| *unit == 0).unwrap_or(buffer.len());
            String::from_utf16_lossy(&buffer[..length])
        })
    };
    let build = read("CurrentBuild").unwrap_or_else(|| "?".to_string());
    match read("DisplayVersion") {
        Some(version) => format!("Windows {version} (build {build})"),
        None => format!("Windows (build {build})"),
    }
}

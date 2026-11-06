//! UTF-8 rotating file log and panic capture.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const MAX_BYTES: u64 = 1_048_576;
const BACKUP_COUNT: u32 = 3;
const LOG_FILENAME: &str = "quickaccess.log";

struct Logger {
    file: File,
    path: PathBuf,
    written: u64,
}

static LOGGER: Mutex<Option<Logger>> = Mutex::new(None);

fn log_directory() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .filter(|value| !value.is_empty())
        .or_else(|| std::env::var_os("APPDATA").filter(|value| !value.is_empty()));
    match base {
        Some(base) => PathBuf::from(base).join("QuickAccess").join("logs"),
        None => std::env::temp_dir().join("QuickAccess").join("logs"),
    }
}

fn open(directory: &Path) -> std::io::Result<Logger> {
    fs::create_dir_all(directory)?;
    let path = directory.join(LOG_FILENAME);
    let file = OpenOptions::new().create(true).append(true).open(&path)?;
    let written = file.metadata()?.len();
    Ok(Logger { file, path, written })
}

fn rotate(logger: &mut Logger) -> std::io::Result<()> {
    let numbered = |index: u32| PathBuf::from(format!("{}.{index}", logger.path.display()));
    let _ = fs::remove_file(numbered(BACKUP_COUNT));
    for index in (1..BACKUP_COUNT).rev() {
        let _ = fs::rename(numbered(index), numbered(index + 1));
    }
    // The open handle must be released before Windows allows the rename.
    let placeholder = File::open(&logger.path)?;
    drop(std::mem::replace(&mut logger.file, placeholder));
    fs::rename(&logger.path, numbered(1))?;
    logger.file = OpenOptions::new().create(true).append(true).open(&logger.path)?;
    logger.written = 0;
    Ok(())
}

/// Open the log, falling back to the temp directory on locked-down profiles,
/// and route panics from any thread into it.
pub fn configure() {
    let logger = open(&log_directory())
        .or_else(|_| open(&std::env::temp_dir().join("QuickAccess").join("logs")))
        .ok();
    *LOGGER.lock().unwrap_or_else(|p| p.into_inner()) = logger;

    std::panic::set_hook(Box::new(|info| {
        write("CRITICAL", &format!("Unhandled panic: {info}"));
    }));
}

pub fn write(level: &str, message: &str) {
    let mut guard = LOGGER.lock().unwrap_or_else(|p| p.into_inner());
    let Some(logger) = guard.as_mut() else {
        return;
    };
    let thread = std::thread::current();
    let line = format!(
        "{} {level} [{}] {message}\n",
        crate::platform::local_timestamp(),
        thread.name().unwrap_or("worker"),
    );
    if logger.written + line.len() as u64 > MAX_BYTES && logger.written > 0 {
        let _ = rotate(logger);
    }
    if logger.file.write_all(line.as_bytes()).is_ok() {
        logger.written += line.len() as u64;
    }
}

#[macro_export]
macro_rules! log_info {
    ($($arg:tt)*) => { $crate::logging::write("INFO", &format!($($arg)*)) };
}

#[macro_export]
macro_rules! log_warn {
    ($($arg:tt)*) => { $crate::logging::write("WARNING", &format!($($arg)*)) };
}

#[macro_export]
macro_rules! log_error {
    ($($arg:tt)*) => { $crate::logging::write("ERROR", &format!($($arg)*)) };
}

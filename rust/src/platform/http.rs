//! Minimal HTTPS GET through WinHTTP, so TLS follows the Windows trust store.

use std::ffi::c_void;

use windows::core::PCWSTR;
use windows::Win32::Networking::WinHttp::{
    WinHttpAddRequestHeaders, WinHttpCloseHandle, WinHttpConnect, WinHttpOpen, WinHttpOpenRequest,
    WinHttpQueryHeaders, WinHttpReadData, WinHttpReceiveResponse, WinHttpSendRequest,
    WinHttpSetTimeouts, WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_ADDREQ_FLAG_ADD,
    WINHTTP_FLAG_SECURE, WINHTTP_OPEN_REQUEST_FLAGS, WINHTTP_QUERY_FLAG_NUMBER,
    WINHTTP_QUERY_STATUS_CODE,
};

use crate::pyish::wide;
use crate::update_rules::{FetchError, MAX_RESPONSE_BYTES};
use crate::url;

const TIMEOUT_MS: i32 = 4000;

struct Handle(*mut c_void);

impl Drop for Handle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: the handle came from WinHTTP and is closed exactly once.
            unsafe {
                let _ = WinHttpCloseHandle(self.0);
            }
        }
    }
}

/// Fetch an HTTPS URL with GitHub API headers, reading a bounded response.
pub fn fetch(target: &str) -> Result<Vec<u8>, FetchError> {
    let parts = url::split(target).map_err(|()| FetchError::Http)?;
    if parts.scheme != "https" {
        return Err(FetchError::Http);
    }
    let host = wide(&parts.hostname());
    let path = wide(&parts.path);
    let agent = wide("QuickAccessLauncher");
    let verb = wide("GET");
    let headers = wide(
        "Accept: application/vnd.github+json\r\nX-GitHub-Api-Version: 2022-11-28\r\n",
    );
    // SAFETY: all strings are NUL-terminated and outlive the calls; handles are
    // wrapped for automatic release, children dropping before their parents.
    unsafe {
        let session = Handle(WinHttpOpen(
            PCWSTR(agent.as_ptr()),
            WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
            PCWSTR::null(),
            PCWSTR::null(),
            0,
        ));
        if session.0.is_null() {
            return Err(FetchError::Offline);
        }
        let _ = WinHttpSetTimeouts(session.0, TIMEOUT_MS, TIMEOUT_MS, TIMEOUT_MS, TIMEOUT_MS);
        let connection = Handle(WinHttpConnect(session.0, PCWSTR(host.as_ptr()), 443, 0));
        if connection.0.is_null() {
            return Err(FetchError::Offline);
        }
        let request = Handle(WinHttpOpenRequest(
            connection.0,
            PCWSTR(verb.as_ptr()),
            PCWSTR(path.as_ptr()),
            PCWSTR::null(),
            PCWSTR::null(),
            std::ptr::null(),
            WINHTTP_OPEN_REQUEST_FLAGS(WINHTTP_FLAG_SECURE.0),
        ));
        if request.0.is_null() {
            return Err(FetchError::Offline);
        }
        let header_units = &headers[..headers.len() - 1];
        let _ = WinHttpAddRequestHeaders(request.0, header_units, WINHTTP_ADDREQ_FLAG_ADD);
        WinHttpSendRequest(request.0, None, None, 0, 0, 0).map_err(|_| FetchError::Offline)?;
        WinHttpReceiveResponse(request.0, std::ptr::null_mut()).map_err(|_| FetchError::Offline)?;

        let mut status = 0_u32;
        let mut size = 4_u32;
        WinHttpQueryHeaders(
            request.0,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            PCWSTR::null(),
            Some(&mut status as *mut u32 as *mut c_void),
            &mut size,
            std::ptr::null_mut(),
        )
        .map_err(|_| FetchError::Http)?;
        if status != 200 {
            return Err(FetchError::Http);
        }

        let mut body = Vec::new();
        let mut chunk = vec![0_u8; 16 * 1024];
        loop {
            let mut read = 0_u32;
            WinHttpReadData(
                request.0,
                chunk.as_mut_ptr().cast(),
                chunk.len() as u32,
                &mut read,
            )
            .map_err(|_| FetchError::Offline)?;
            if read == 0 {
                return Ok(body);
            }
            body.extend_from_slice(&chunk[..read as usize]);
            // Bound untrusted input: one byte over the limit is enough for the
            // caller to reject the response.
            if body.len() > MAX_RESPONSE_BYTES {
                return Ok(body);
            }
        }
    }
}

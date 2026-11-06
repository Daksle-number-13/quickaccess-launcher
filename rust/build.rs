fn main() {
    println!("cargo:rerun-if-changed=resources/quickaccess.rc");
    println!("cargo:rerun-if-changed=resources/quickaccess.manifest");
    println!("cargo:rerun-if-changed=../assets/quickaccess.ico");
    embed_resource::compile("resources/quickaccess.rc", embed_resource::NONE)
        .manifest_required()
        .expect("embedding the Windows resources failed");
}

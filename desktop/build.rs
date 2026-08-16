use std::env;
use std::path::Path;

fn main() {
    println!("cargo:rerun-if-changed=assets/deerflow_icon.ico");

    if env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let icon = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("assets")
            .join("deerflow_icon.ico");
        let icon = icon
            .to_str()
            .expect("DeerFlow icon path must contain valid Unicode");

        winresource::WindowsResource::new()
            .set_icon(icon)
            .compile()
            .expect("failed to embed the DeerFlow Windows icon");
    }
}

#!/usr/bin/env python3
"""Package and install the local VS Code bridge without Node/npm dependencies."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser(description="将本地 codex-auth 桥接扩展安装到当前 VS Code")
    parser.add_argument("--code", default="code", help="VS Code 命令，默认 code")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    metadata = json.loads((root / "package.json").read_text())
    manifest = f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
<Metadata><Identity Language="en-US" Id="{metadata['name']}" Version="{metadata['version']}" Publisher="{metadata['publisher']}"/>
<DisplayName>{metadata['displayName']}</DisplayName><Description xml:space="preserve">{metadata['description']}</Description>
<Categories>Other</Categories></Metadata><Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
<Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>'''
    content_types = '''<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/>
<Default Extension="vsixmanifest" ContentType="text/xml"/></Types>'''
    with tempfile.TemporaryDirectory(prefix="codex-auth-vsix-") as tmp:
        vsix = Path(tmp) / "codex-auth-bridge.vsix"
        with zipfile.ZipFile(vsix, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("[Content_Types].xml", content_types)
            package.writestr("extension.vsixmanifest", manifest)
            package.write(root / "package.json", "extension/package.json")
            package.write(root / "extension.js", "extension/extension.js")
        result = subprocess.run([args.code, "--install-extension", str(vsix), "--force"], check=False)
        if result.returncode:
            print("桥接扩展安装失败。请确认在目标 VS Code 的终端中运行此命令。", file=sys.stderr)
            return result.returncode
    print("桥接扩展已安装。若尚未生效，请在 VS Code 中执行一次“开发人员: 重启扩展主机”。")
    print("启用自动重启：codex-auth vscode enable")
    return 0


if __name__ == "__main__":
    sys.exit(main())

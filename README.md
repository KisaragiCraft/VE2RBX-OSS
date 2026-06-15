# VE2RBX OSS

VE2RBX OSS is a local VoxEdit-to-Roblox converter.

It runs only on your PC. No account, billing, cloud worker, Supabase, R2, Render, Cloudflare, or Stripe setup is required.

## What It Does

- Opens a local browser UI on `127.0.0.1`
- Accepts one VoxEdit project folder or one ZIP file
- Converts the project locally with the bundled VE2RBX converter scripts
- Writes converted files to `Documents\VE2RBXoutput`
- Keeps temporary working files on the local machine

The selected folder or ZIP must contain a complete VoxEdit project with both `.vxr` and `.vxa` files. A `.vxm` mesh file by itself is not enough for this converter flow.

Use English letters and numbers for VoxEdit project folder names. The project name is used for exported filenames, and English names avoid Roblox texture-reference issues.

## 日本語クイックスタート

1. GitHub Releases から `VE2RBX.exe` をダウンロードします。
2. `VE2RBX.exe` を起動します。
3. ブラウザで開いた画面に VoxEdit プロジェクトフォルダ、または ZIP をドロップします。
4. 変換ボタンを押します。
5. 出力は `Documents\VE2RBXoutput` に作成されます。

VoxEdit プロジェクト名は英数字を推奨します。日本語名でも処理できる場合がありますが、Roblox Studio 側でテクスチャ参照が崩れることがあります。

## Download And Run

### Recommended: Windows EXE

1. Download `VE2RBX.exe` from GitHub Releases.
2. Run `VE2RBX.exe`.
3. When the browser opens, drop a VoxEdit project folder or ZIP file.
4. Press the convert button.
5. Open the generated folder under:

```text
%USERPROFILE%\Documents\VE2RBXoutput\
```

### Run From Source ZIP

Requirements:

- Windows
- Python 3.11 or newer
- Blender installed, or `blender` / `blender.exe` available on PATH

Download the source ZIP from GitHub, extract it, then run:

```powershell
python app\launcher\local_launcher.py
```

For a no-browser smoke test:

```powershell
python app\launcher\local_launcher.py --no-browser --port 8765
```

## Output Files

Each conversion creates a project-named folder in:

```text
%USERPROFILE%\Documents\VE2RBXoutput\
```

Typical files:

- `<ProjectName>.fbx` - static model for Roblox Studio, exported at 0.01 scale
- `<ProjectName>.glb` - static GLB, kept at normal scale
- `<ProjectName>_obj\` - OBJ/MTL/texture output, kept at normal scale
- `<ProjectName>_anim.fbx` - animation rig/model when animation export is available
- `<ProjectName>_animclip_<ClipName>.fbx` - per-clip animation FBX files
- `<ProjectName>_roblox_post_import.luau` - helper script for imported visual models

## Roblox Studio Notes

For visual models, import `<ProjectName>.fbx` into Roblox Studio. After import, run `<ProjectName>_roblox_post_import.luau` from the command bar if you want imported MeshParts anchored and visual-only.

For animations, import `<ProjectName>_anim.fbx` as the animation edit rig/model first. Then assign each `<ProjectName>_animclip_<ClipName>.fbx` from Roblox Studio's animation clip editor workflow.

## Build The EXE

This is only needed if you want to create your own local Windows executable.

Install PyInstaller, then run:

```powershell
python -m pip install pyinstaller
.\build_win.bat
```

The generated file is:

```text
dist\VE2RBX.exe
```

## Runtime Folders

User-facing outputs:

```text
%USERPROFILE%\Documents\VE2RBXoutput\
```

Temporary files when running from source:

```text
runtime\
```

Temporary files when running from the packaged EXE:

```text
%LOCALAPPDATA%\VE2RBX OSS\
```

These temporary folders are not required for GitHub distribution.

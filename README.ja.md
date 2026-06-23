# VE2RBX OSS

**VoxEdit / The Sandbox の制作資産を、Roblox で再利用。** VE2RBX OSS は、VoxEdit プロジェクトを Roblox にそのまま使える `FBX` / `GLB` / `OBJ` へ変換するローカルツールです。色・発光・アニメーションを保ったまま変換し、すべて PC 内で完結します（アカウント不要・支払い不要・どこにもアップロードしません）。

*Other languages: [English](README.md)*

<p align="center">
  <img src="assets/demo_ja.gif" width="100%" alt="VE2RBX デモ — VoxEdit の作品を Roblox へ変換">
</p>

<p align="center"><sub>VoxEdit / The Sandbox の作品を Roblox Studio へ — 色・発光・アニメそのまま。 &nbsp;·&nbsp; <a href="https://github.com/KisaragiCraft/VE2RBX-OSS/releases">VE2RBX.exe をダウンロード →</a></sub></p>

<table>
  <tr>
    <td align="center"><strong>1. あなたの VoxEdit プロジェクト</strong></td>
    <td align="center"><strong>2. 標準 FBX エクスポート</strong></td>
    <td align="center"><strong>3. VE2RBX</strong></td>
  </tr>
  <tr>
    <td><img src="assets/compare_color_VoxEdit.png" width="100%"></td>
    <td><img src="assets/compare_color_Before.png" width="100%"></td>
    <td><img src="assets/compare_color_After.png" width="100%"></td>
  </tr>
</table>

<sub>既存の VoxEdit 資産を、色を保ったまま Roblox Studio へ。崩れたジオメトリも、ボケた境界もありません。</sub>

## VE2RBX を使う理由

VoxEdit で作ったモデルを Roblox で使いたい——でも通常のエクスポートだと色やテクスチャが崩れ、手直しに時間が掛かりがちです。VE2RBX は単なるファイル変換ではなく、「Roblox Studio が実際にどうモデルを取り込むか」を踏まえて設計されています。

**色がにじまない、境界が鮮明。**
VoxEdit のモデルはピクセルアートのように、各面がはっきりした単色で構成されています。通常のテクスチャベイクはこの境界をぼかしてしまいますが、VE2RBX は各色ごとに面を分け、その面をパレットの単色で塗りつぶすため、境界は鮮明なまま・他の色が一切混ざりません。

**発光（エミッシブ）を、狙った所だけ。**
VoxEdit の発光色は Roblox へ直接移せず、Roblox の `SurfaceAppearance` は付けたモデル全体を光らせてしまいます。VE2RBX は発光色を含むパーツだけを別モデルに分離するので、そこにだけ `SurfaceAppearance` を入れれば、モデル全体ではなく光らせたいパーツだけを発光させられます。

<table>
  <tr>
    <td align="center"><strong>取り込んだ状態（SurfaceAppearance なし）</strong></td>
    <td align="center"><strong>SurfaceAppearance を追加（夜の表示）</strong></td>
  </tr>
  <tr>
    <td><img src="assets/compare_glow_Before.png" width="100%"></td>
    <td><img src="assets/compare_glow_After.png" width="100%"></td>
  </tr>
</table>

**アニメーションもそのまま。**
VoxEdit のアニメーションを Roblox へそのまま移送できます。

<div align="center"><img src="assets/anim.gif" width="480"></div>

**Roblox を意識したジオメトリ整理。**
Roblox は1メッシュあたり 20,000 頂点が上限です。VE2RBX は VoxEdit の細かいパーツをマージして大量のモデルが取り込まれないよう整理しつつ、上限に達する前に自動で別パーツへ分割します。アニメーション付きモデルでは、動くパーツは分離し、動かないパーツはマージします。

**完全ローカル。**
アカウント不要・課金不要。プロジェクトが PC の外に出ることはありません。

> 出力形式: `.fbx` / `.glb` / `.obj`

## ギャラリー

VE2RBX で変換した作例:

<table>
  <tr>
    <td><img src="assets/hero_After1.png" width="100%"></td>
    <td><img src="assets/hero_After2.png" width="100%"></td>
  </tr>
</table>

## クイックスタート

1. [Releases ページ](https://github.com/KisaragiCraft/VE2RBX-OSS/releases) から `VE2RBX.exe` をダウンロード。
2. 起動すると、ブラウザに変換画面が開きます。
3. VoxEdit プロジェクトの **フォルダまたは `.zip`** をドロップ。
4. **Convert** を押す。
5. 出力は `Documents\VE2RBXoutput` に作成されます。

**入力について:** フォルダ／ZIP は、`.vxr` と `.vxa` の両方を含む*完全な* VoxEdit プロジェクトである必要があります（`.vxm` メッシュ単体では不可）。プロジェクトフォルダ名は英数字を推奨します——名前が出力ファイル名に使われ、日本語名だと Roblox 側でテクスチャ参照が崩れることがあります。

## 出力ファイル

`Documents\VE2RBXoutput` 内に、プロジェクト名のフォルダが作られます:

| ファイル | 内容 |
| --- | --- |
| `<Name>.fbx` | Roblox Studio 用の静的モデル（0.01 スケールで出力） |
| `<Name>.glb` | 通常スケールの静的 GLB |
| `<Name>_obj/` | 通常スケールの OBJ + MTL + テクスチャ |
| `<Name>_anim.fbx` | アニメーションのリグ/モデル（アニメ出力がある場合） |
| `<Name>_animclip_<Clip>.fbx` | アニメクリップごとの FBX |
| `<Name>_roblox_post_import.luau` | 取り込んだ表示用モデル向けの補助スクリプト |

## Roblox Studio での使い方

**静的モデル:** `<Name>.fbx` を取り込みます。取り込み後、MeshPart を固定・表示専用にしたい場合は、コマンドバーから `<Name>_roblox_post_import.luau` を実行してください。

**アニメーション:** まず `<Name>_anim.fbx` をアニメーション用のリグ/モデルとして取り込み、各 `<Name>_animclip_<Clip>.fbx` を Roblox Studio のアニメーションクリップエディタで割り当てます。

**発光:** 発光色を含むモデルでは、分離された発光パーツに `SurfaceAppearance` を追加すると、そのパーツだけを光らせられます。

## ソースから実行

日常利用は上記の EXE が簡単です。ソースから実行する場合:

**必要環境**
- Windows
- Python 3.11 以上
- Blender がインストール済み、または `blender` / `blender.exe` が PATH 上にあること

```powershell
python app\launcher\local_launcher.py
```

同じローカル変換 UI が開き、出力は `Documents\VE2RBXoutput` に書き出されます。ブラウザなしの動作確認:

```powershell
python app\launcher\local_launcher.py --no-browser --port 8765
```

### EXE を自分でビルド

```powershell
python -m pip install pyinstaller
.\build_win.bat
```

生成物は `dist\VE2RBX.exe` です。

## ライセンス

VE2RBX OSS は **GNU General Public License v3.0 or later（`GPL-3.0-or-later`）** で公開されるフリーソフトウェアです。全文は [LICENSE](LICENSE) を参照してください。

- **本ツールで生成した `.fbx` / `.glb` / `.obj` はあなたのものです。** 出力物は本ライセンスの制約を受けず、商用 Roblox 作品でも自由に利用できます。
- 変換処理は Blender の Python API（`bpy`）を通じて動作するため、VE2RBX OSS 自体は Blender のライセンスと整合させる目的で GPL の下で配布します。改変・再配布する場合は、同じ条件で公開してください。

Copyright (C) 2026 KisaragiCraft

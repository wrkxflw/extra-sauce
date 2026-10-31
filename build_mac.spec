# -*- mode: python ; coding: utf-8 -*-
# macOS build: pyinstaller --noconfirm build_mac.spec -> dist/Extra Sauce.app
from PyInstaller.utils.hooks import collect_data_files

datas = [('app/acr3_curve.txt', 'app'), ('app/web', 'app/web')]
datas += collect_data_files('webview')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['webview.platforms.cocoa'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinterdnd2'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ExtraSauce',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ExtraSauce')
app = BUNDLE(
    coll,
    name='Extra Sauce.app',
    icon='app/assets/icon.icns',
    bundle_identifier='com.wrkxflw.extrasauce',
    info_plist={
        'CFBundleShortVersionString': '1.0',
        'CFBundleDisplayName': 'Extra Sauce',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '12.0',
    },
)

# FFCC Importer
<img width="1000" height="600" alt="image" src="https://github.com/user-attachments/assets/cdcbe8bf-0a02-4694-9639-be8e391548ff" />

Addon for Blender 5.x+ that imports 3D scene (.mpl) and character models (.chm) from Final Fantasy Crystal Chronicles for the GameCube.

Need help with this addon? Join the [Realitea Discord](https://discord.gg/43ggeGC8A8) and ask for help.

## Features
Both scene models (.mpl) and character models (.chm) can be imported. All textures are imported automatically, and for characters, the skeleton (with animations if any are found) is also imported.

Import settings allow you to customize some quality-of-life features.
- **Blend Vertex Colors** - automatically blends FFCC's original baked ambient lighting onto albedo textures, for any mesh that has vertex colors
- **Disable Color Correction** - adjusts the Color Management settings in your scene to sRGB / Standard, so colors match the original colors from PSO

## Installation
1. Click the green "Code" button above and press "Download ZIP"
2. Go into Blender's addon preferences (File → Preferences → Addons)
3. Click the <img width="20" height="21" alt="image" src="https://github.com/user-attachments/assets/92cefcff-c9d0-4c29-b1ef-a7efe9d07016" /> button on the top right of the window, and select "Install from Disk..."
4. Browse to the ZIP file you just downloaded, select it, and press Return/Enter.
   
You can find the importers via Blender's "_FIle_" -> "_Import_" menu.

## Tips
- You can find character models in the game disc's "char" folder, split up into various categories (like "npc", "pc" for player characters, "wp" for weapons, etc.)
- Scene models can be found in the "map" folder on the game disc. Make sure you extract all files for each folder, not just the .mpl, otherwise your import may be missing textures.
- Animations can be found / assigned via the Dope Sheet's Action Editor. Select your armature first in the 3D viewport, then select the action in the Action Editor. If animations don't play, you may need to assign the "Object" in the Slot setting (second dropdown menu at the top of the action editor), which was added in Blender 4.4.

## Previews

<img width="720" height="405" alt="1" src="https://github.com/user-attachments/assets/568a8b03-1a2e-421c-be23-2a0d766fb96b" />
Scene imports

<img width="1000" height="600" alt="2" src="https://github.com/user-attachments/assets/2b311b31-deb5-492e-84d5-a35286fce87e" />
Character model imports - this includes player characters, weapons, bosses, enemies, and NPCs

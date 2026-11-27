Is an app companion for Linux built with python, that will display artwork and lyrics integrates winamp and popamp skins
<img width="1183" height="1028" alt="image" src="https://github.com/user-attachments/assets/bfc03d3b-e77a-4288-968a-1b625026f3d5" />

<img width="1689" height="649" alt="image" src="https://github.com/user-attachments/assets/16b98d89-895f-4e0d-aed0-d0d2b4b3c7c2" />
**Dependencies**
```
sudo pacman -S python-gobject gtk3 python-mutagen python-pillow --needed
```

**Installation**

Download and copy the popamp_aurora.py file somewhere you want to store it

Make it executable:
```
chmod +x popamp_aurora.py
```

Create a launcher:
```
vim ~/.local/share/applications/popamp-aurora.desktop
```

**Example contents**

Change the location of the file acordingly and the location of the popamp_album_list.py file"

```
[Desktop Entry]
Version=1.0
Type=Application
Name=Popamp Aurora
Comment=Large album artwork and synchronized lyrics for Popamp
Exec=python3 /home/@YOUR_USER/@LOCATION/popamp-albumlist/popamp_aurora.py
Icon=application-x-fictionbook
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupNotify=true
StartupWMClass=popamp-aurora
Path=
```


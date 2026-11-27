Is an app companion for Linux built with python, that will display artwork and lyrics integrates winamp and popamp skins

<img width="1137" height="1023" alt="image" src="https://github.com/user-attachments/assets/69c752a7-a9aa-4a45-bb67-cafe05843a2f" />


<img width="1841" height="1036" alt="image" src="https://github.com/user-attachments/assets/1c2b5f0b-27fd-4b8c-b186-6407354cfb18" />


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


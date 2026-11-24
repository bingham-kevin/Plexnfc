# Marquee Remote for iPhone and iPad

Choose which TV the movie book plays on when you tap it with this phone. Each paired iPhone or iPad picks its own player; taps from other phones keep going to plexnfc's default player. It also shows what's playing on that player, with pause, ±30 seconds and stop.

## How it works

1. Choose the Movie Book server from **On this network** (plexnfc announces itself with Bonjour as `_moviebook._tcp`), or enter its address.
2. The app shows a 6-digit code. In Home Assistant's **Movie Book** panel, name the phone and approve the code.
3. Pick a player: a Marquee Apple TV. **Default** follows the server's setting.

A tapped tag opens in Safari, not in the app, so plexnfc recognizes the phone by the Wi-Fi address the app last used (`/phone/players`, `/phone/player`). Open Marquee Remote after joining the home Wi-Fi, and set the network's **Private Wi-Fi Address** to **Fixed** on the phone, so the address stays the same.

Taps for a room other than the default player's run that player's own get-ready script, from plexnfc's **Get-ready scripts for other players** setting (see the app's documentation in Home Assistant).

## Install

You need a Mac with Xcode and an Apple ID.

1. Open `MarqueeRemote/MarqueeRemote.xcodeproj` in Xcode.
2. Select the **MarqueeRemote** target, open **Signing & Capabilities** and pick your team.
3. Connect the iPhone or iPad with a cable (or pair it in **Window → Devices and Simulators**), turn on **Developer Mode** on it when asked, choose it as the run destination and press Run.
4. The first time it opens, allow **Local Network** access so it can find and reach the server.

With a free Apple ID the app runs for 7 days before it has to be installed again from Xcode.

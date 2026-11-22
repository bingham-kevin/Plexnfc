# Submitting Marquee to the App Store

What to send Apple, and how the app is set up to meet the App Store Review Guidelines.

## Before you submit

- **Apple Developer Program membership** ($99/year). A free Apple ID can install on your own Apple TV but can't submit.
- **A unique bundle ID** in your developer account. `com.binghamkevin.moviebook` is fine if it's yours; it can't change after the first upload.
- **The app name**: "Marquee" may already be taken on the App Store. If App Store Connect says so, use a subtitle-style name such as "Marquee: Movie Night", and keep the Home screen name (`CFBundleDisplayName`) as "Marquee".
- **A privacy policy URL**: publish `tvos/PRIVACY.md`, for example on GitHub Pages, and paste the link into App Store Connect.
- **Screenshots**: 1920×1080 or 3840×2160, taken in the simulator (Home, a movie playing with the poster card, Search, Settings).
- **Archive and upload**: in Xcode, choose **Product → Archive**, then **Distribute App → App Store Connect**.

## App Store listing (draft)

- **Subtitle**: Your movies, one tap away
- **Category**: Entertainment
- **Description**:

  > Marquee plays movies from your own home media library on Apple TV.
  >
  > Browse your library in poster rows, search by title, and press play. With a printed Movie Book of posters and NFC stickers, tapping a page with your phone starts that movie on your TV.
  >
  > • Your library: Recently Added, collections and every movie, with posters
  > • Search
  > • Apple's own player, with the Siri Remote and the info panel
  > • Starts with subtitles off
  > • Pairs with your server using a one-time code
  > • A demo library of free films, so you can try it without a server
  >
  > Marquee needs a Movie Book server on your home network for your own library. It collects no data.

- **Keywords**: movies, home theater, media library, poster, NFC, movie night
- **Support URL**: https://github.com/bingham-kevin/Plexnfc

## Notes for App Review (paste into App Store Connect)

> Marquee plays movies from the user's own media server on their home network, which the reviewer won't have. To review without a server, choose **Try the Demo Library** on the first screen: it shows the app's full browsing and playback with three freely licensed films and clips (Blender Foundation open movies, CC BY 3.0) streamed from public test servers. Search and Settings work in the demo too.
>
> With a server, the user enters its address, Marquee shows a 6-digit code, and the user approves it in the server's own web page; only approved Apple TVs can browse or play.

## App Privacy answers

- **Data collection**: "No, we do not collect data from this app."
- **Tracking**: none.

## How the app meets the guidelines

| Guideline | How Marquee meets it |
|---|---|
| 2.1 App completeness | The demo library lets Review use every screen without a server. |
| 2.3 Accurate metadata | The listing says a home server is needed for your own library. |
| 4.0 Design | Siri Remote and focus navigation throughout; Apple's own player; layered icon and top shelf images. |
| 4.2 Minimum functionality | Browsing, collections, search and playback work in the app itself, not only through taps. |
| 5.1.1 Data collection and storage | No data collected; the pairing secret is in the Keychain; a privacy policy is linked. |
| 5.1.2 Data use and sharing | Talks only to the user's own server and, for the demo, public test-stream servers. |
| 5.2 Intellectual property | No third-party media-server names or logos in the app or listing; demo films are CC BY with credits in Settings; demo posters were drawn for the app. |
| Export compliance | `ITSAppUsesNonExemptEncryption` is `NO`: only Apple's built-in HTTPS. |

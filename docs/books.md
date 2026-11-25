# Books

The control panel's **Books** section plays audiobooks from [Audiobookshelf](https://www.audiobookshelf.org) on the same AirPlay, Cast and Music Assistant speakers as music, picks up where you left off, and links NFC cards to books. Like **Movies** and **Music**, it has four pages: **Library**, **Cards**, **Book** and **Sources**.

## Connecting Audiobookshelf

Under **Books › Sources**, give Marquee:

- Audiobookshelf's address, such as `http://nas.local:13378`.
- An API key. In Audiobookshelf, open **Settings › API Keys** and add one called Marquee.

Marquee tries the key before saving it, in `audiobookshelf.json` beside its other settings (readable only by you). **Disconnect** forgets it. `AUDIOBOOKSHELF_URL` and `AUDIOBOOKSHELF_TOKEN` in `.env` do the same. Only book libraries are shown; podcast libraries are left out.

## Listening

**Books › Library** shows your books, authors and series. Open a book to see how far you've got, its chapters and its description, then choose one or more speakers and:

- **Resume at 1:23:45** carries on from where Audiobookshelf says you are, whichever app you were listening in.
- **From the start** starts again.
- ▶ beside a chapter plays from that chapter.

While a book plays, Marquee tells Audiobookshelf where you are every 15 seconds, and when you pause or stop, so its apps pick up from there too. A book is marked finished in its last few seconds.

AirPlay speakers start partway through a file with ffmpeg; without ffmpeg on the server, an AirPlay speaker starts at the beginning of the chapter's file. Cast and Music Assistant speakers seek on their own.

## Book cards

**Books › Cards** links a card to each book, the way **Movies › Cards** does for movies: choose **All books**, **An author's** or **A series**, untick what you don't want, and **Scan cards**. Hold a blank card on a Movie Remote (or an NFC reader in Home Assistant) as each book comes up.

To link one card, open the book in **Books › Library** and use **Link next card** or **Link card XX-XX-XX-XX**.

Tapping a book card plays the book from where you left off, on the speakers of the reader it's held to (its default output under **Devices › Readers**).

## A printable book of audiobooks

**Books › Book** makes a "Story Time" book: a cover per page (or four), with the author, narrator, series and length, and a gold circle where the card goes. Choose every audiobook, an author's or a series, and print it at 100% with background graphics on. With **QR code** on, each book with a card gets a code that plays it from a phone.

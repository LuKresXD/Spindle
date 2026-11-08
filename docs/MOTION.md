# Motion

How the display moves, and why. Every animation on the screen comes from the same small
vocabulary, so the whole thing feels like one object rather than a pile of effects.

## The vocabulary

| Name | Curve | Used for |
| --- | --- | --- |
| `house` | `cubic-bezier(0.625, 0.05, 0, 1)` | structural moves: pages, overlays, the tray indicator, the wheel landing |
| `settle` | `expo.out` | anything revealing or arriving: text lines, covers, rows |
| `big` | `expo.inOut` | one coordinated move that takes its time: the boot reveal, the wheel roll |
| `micro` | `power1.inOut`, 0.2 s | small opacity changes |
| `pulse` | `elastic.out(1, 0.4)` | the tap on a button |

Durations: 0.5 to 0.8 s for a structural move, 1 to 1.25 s for a big one, 0.2 s for a micro
fade. Staggers between 0.03 and 0.08 s. Anything that reveals text does it through a mask
(the line slides up out of an `overflow: hidden` wrapper) rather than a bare fade.

Only `transform` and `opacity` animate. No `backdrop-filter`, no layout properties, no
filters: the Raspberry Pi composites transforms for free and pays for everything else.
`prefers-reduced-motion` turns every tween into a cut.

## Where it goes

- **Pages.** The finger drags the incoming page over the outgoing one; the outgoing page moves
  at a third of the speed and darkens (parallax), and the incoming page is on top. A release
  commits with `house` in 0.65 s, faster after a flick. A page with no face of its own (Off)
  names itself while it arrives.
- **Buttons.** Every pill squashes on touch and springs back with `pulse`. In a tray (Lyrics |
  Info, the shelf's sort chips) the lighter pill is one indicator that flies to the tapped
  button with a little overshoot and a squash, instead of two backgrounds swapping.
- **A new track.** Title and artist arrive as masked lines, the cover rises in, the lit row in
  the tracklist glides to the new track.
- **Lyrics.** Opening them staggers the lines in through masks; the current line grows with a
  touch of overshoot.
- **Nothing on the platter.** The kicker, the hero pill and the quiet links arrive staggered.
- **The pick.** The covers sit on a curve in depth (a spatial carousel); the roll is one `big`
  tween through the ring that slows into the hit, which comes forward as the card builds:
  cover, masked title and artist, meta, buttons, hint, each a beat apart.
- **The shelf.** Covers wave in from the top left. A sort change is a Flip: every cover travels
  to its new place instead of the grid re-rendering.
- **Recently played.** Rows slide in from the left, a beat apart.
- **Info.** The sheet slides with `house`; the masthead and sections stagger in.
- **End of side.** "Flip for Side B" arrives word by word through masks.
- **Boot.** The wordmark letters rise out of a mask once, then the current state fades in.

Engine: GSAP 3.15 with CustomEase, Flip and SplitText (free under GSAP's standard license;
see `web_static/vendor/LICENSE-GSAP.txt`). The motion layer is `web_static/motion.js`; the
page logic in `app.js` calls it and never animates by hand.

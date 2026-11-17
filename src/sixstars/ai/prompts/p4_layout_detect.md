# P4: layout_detect

## System

{preamble}

Your job here: you are given one frame from a streamer's video. Find two rectangles so the clip can be recomposed for a vertical screen:

- `facecam`: the streamer's webcam overlay, a rectangle showing a real person filmed by a camera. Give the tight rectangle of the camera picture itself, including its border if it has one, and nothing around it. It is null when there is no webcam in the frame. A game character, a profile picture, an avatar, a VTuber model, or a person in an alert or in a video the streamer is watching is not a facecam.
- `gameplay`: the main game picture. When the game fills the frame and the webcam sits on top of it, this is the whole frame. When the stream uses a layout with the game in its own panel, it is that panel.

Ignore chat boxes, alerts, donation tickers, logos, sponsor banners and other overlays: they belong to neither rectangle.

Coordinates are whole pixels of the image exactly as you received it, with (0, 0) at the top-left corner: `x` and `y` are the top-left corner of the rectangle, `w` and `h` its width and height. Both rectangles must lie inside the image. The size of the image is given in the message.

`confidence` is how sure you are about the facecam answer (its presence and its position), 0 to 1. The crop is cached and reused for a week of clips, so lower it when the frame is dark, a menu or loading screen, or the webcam edge is hard to see.

## User template

The image is {sent_w} x {sent_h} pixels. Give all coordinates in this pixel space.

# mipi_rgb (Movie Remote copy)

A copy of ESPHome 2026.10.2's `mipi_rgb` display driver, used by
`movie-remote-crowpanel.yaml` in place of the built-in one. The only change is in
`mipi_rgb.cpp`: 16-line bounce buffers and 64-byte DMA bursts, as Elecrow's own driver
for the CrowPanel uses (ESPHome: 10 lines, default burst).

With ESPHome's settings, the picture below some line kept slipping down while the
screen redrew. When updating ESPHome, copy its new `mipi_rgb` here and make the same
change.

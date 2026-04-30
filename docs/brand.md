# Brand Assets

<p align="center">
  <img src="../CliCourierLogo.png" alt="CliCourier - vibe code everywhere" width="520">
</p>

CliCourier uses the purple-to-blue delivery gradient from the logo as its primary visual
system. Use the full logo when the slogan has room to breathe, such as the GitHub README
or release notes. Use the small logo for compact docs, icons, cards, and places where the
`vibe code everywhere` slogan would be too small.

## Logo Files

| File | Size | Use |
| --- | ---: | --- |
| [`CliCourierLogo.png`](../CliCourierLogo.png) | 1254 x 1254 | Full GitHub and docs lockup with slogan. |
| [`CliCourierLogoSmall.png`](../CliCourierLogoSmall.png) | 652 x 499 | Compact mark without slogan. |

## Palette

These colors were extracted from the supplied PNGs. Because the artwork uses gradients
and antialiasing, the accent colors are exact sampled pixels from visible brand regions,
while the navy/background colors are exact dominant pixels from the logo histogram.

| Token | Hex | RGB | Source |
| --- | --- | --- | --- |
| Courier navy | `#051024` | `rgb(5, 16, 36)` | Dominant terminal body pixel in `CliCourierLogoSmall.png`. |
| Chrome navy | `#172439` | `rgb(23, 36, 57)` | Dominant terminal title-bar pixel in `CliCourierLogoSmall.png`. |
| Courier purple | `#5F45E4` | `rgb(95, 69, 228)` | Sampled package-gradient pixel in `CliCourierLogoSmall.png`. |
| Motion violet | `#5429CA` | `rgb(84, 41, 202)` | Sampled purple motion-streak pixel in `CliCourierLogoSmall.png`. |
| Courier blue | `#2B6CF1` | `rgb(43, 108, 241)` | Sampled package-gradient pixel in `CliCourierLogoSmall.png`. |
| Motion blue | `#1789F9` | `rgb(23, 137, 249)` | Exact blue motion-streak pixel in `CliCourierLogoSmall.png`. |
| Soft white | `#FEFDFE` | `rgb(254, 253, 254)` | Dominant off-white background pixel in `CliCourierLogoSmall.png`. |

Use `#5F45E4 -> #2B6CF1 -> #1789F9` for primary gradients, `#051024` for dark text or
terminal surfaces, and `#FEFDFE` for light backgrounds.

## Extraction

Dominant colors were checked with ImageMagick:

```bash
convert CliCourierLogoSmall.png -alpha off -format %c histogram:info:- | sort -nr | head -40
convert CliCourierLogoSmall.png -alpha off -colors 16 -format %c histogram:info:- | sort -nr
```

Sampled accent pixels were checked with:

```bash
convert CliCourierLogoSmall.png -format '%[pixel:p{330,380}] %[pixel:p{419,130}] %[pixel:p{430,360}] %[pixel:p{428,456}]' info:
```

# Shared, theme-aware styling helpers for every mini-game -- so a game's
# outer shell, buttons, and grid panels ALL adopt the app's currently
# selected theme (colors AND live switching) automatically and
# consistently, with real visual weight (thick borders, padding, distinct
# hover/pressed states) instead of each game hand-rolling its own ad hoc,
# often non-themed, 1px-outline styling that reads as flimsy and ignores
# whichever theme the user actually picked.
#
# A game's actual PLAYING SURFACE can still want its own deliberate
# palette where a real, universally-recognized convention needs it (e.g.
# Minesweeper's classic revealed-cell number colors only read correctly
# against a light board) -- but even that surface is a TINT derived from
# the theme's own colors (tinted_surface()), not a flat hardcoded hex, so
# it still visibly shifts with the app's theme instead of staying a fixed
# light-mode island inside a dark app.


def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def tinted_surface(colors, base_key="accent", alpha=40):
    """A translucent tint of one of the theme's own colors -- e.g. Sudoku's
    given/clue cells: a soft "this one's special" fill that shifts with
    the theme instead of being a fixed hex value. alpha is 0-255. This is
    an OVERLAY -- Qt composites it against whatever's rendered behind the
    widget, so the result depends on that backdrop; for content that needs
    a guaranteed-light (or guaranteed-dark) result regardless of the
    theme's own darkness, use light_tint()/mix_hex() instead."""
    r, g, b = _hex_to_rgb(colors[base_key])
    return f"rgba({r}, {g}, {b}, {alpha})"


def mix_hex(hex_a, hex_b, weight):
    """Linear-interpolates two hex colors per RGB channel (weight=0 -> a,
    weight=1 -> b) into a plain, OPAQUE hex string -- backdrop-independent,
    unlike tinted_surface()'s alpha overlay."""
    ra, ga, ba = _hex_to_rgb(hex_a)
    rb, gb, bb = _hex_to_rgb(hex_b)
    mix = lambda x, y: round(x + (y - x) * weight)
    return f"#{mix(ra, rb):02X}{mix(ga, gb):02X}{mix(ba, bb):02X}"


def light_tint(colors, base_key="accent", weight=0.82):
    """A genuinely light, opaque tint of one of the theme's colors mixed
    toward white -- for content (e.g. Minesweeper's revealed cells) that
    needs a guaranteed-light surface for a classic dark-on-light color
    convention to read correctly, no matter how dark the active theme is."""
    return mix_hex(colors[base_key], "#FFFFFF", weight)


def panel_style(colors, radius=10):
    """The shared outer "card" look every mini-game gets automatically
    (see base_game.MiniGameWidget) -- a real bordered, padded panel
    instead of bare, borderless content floating on the window background."""
    return (
        f"background-color: {colors['surface']}; "
        f"border: 3px solid {colors['border']}; "
        f"border-radius: {radius}px;"
    )


def board_frame_style(colors, radius=8):
    """A distinct inset frame around a game's actual grid -- visually
    separates the playing surface from the panel chrome around it (status
    label, tool buttons)."""
    return (
        f"background-color: {colors['bg']}; "
        f"border: 2px solid {colors['border']}; "
        f"border-radius: {radius}px;"
    )


def heading_style(colors, size=18):
    return f"color: {colors['accent']}; font-size: {size}px; font-weight: bold;"


def status_style(colors, size=14):
    return f"color: {colors['text']}; font-size: {size}px;"


def button_style(colors, size=15, radius=6, bold=True):
    """A COMPLETE button stylesheet -- normal/hover/pressed/disabled all
    spelled out explicitly, never a partial override -- since a per-widget
    stylesheet that only sets e.g. font-size risks losing the app-level
    stylesheet's :hover/:pressed rules depending on Qt's cascade behavior
    for that widget. Every themed game button goes through this (or
    primary_button_style()) so state coverage is always complete."""
    weight = "bold" if bold else "normal"
    return f"""
        QPushButton {{
            background-color: {colors['surface']};
            color: {colors['text']};
            border: 2px solid {colors['border']};
            border-radius: {radius}px;
            font-size: {size}px;
            font-weight: {weight};
            padding: 4px;
        }}
        QPushButton:hover {{
            background-color: {colors['surface_hover']};
            border-color: {colors['accent']};
        }}
        QPushButton:pressed {{
            background-color: {colors['selection_bg']};
            color: {colors['selection_text']};
            border-color: {colors['selection_bg']};
        }}
        QPushButton:disabled {{
            color: {colors['text_dim']};
            border-color: {colors['text_dim']};
        }}
    """


def toggle_button_style(colors, size=15, radius=6):
    """Like button_style(), but with an explicit :checked state -- for a
    checkable QPushButton (e.g. Sudoku's Notes toggle) where the app-level
    stylesheet's own :checked rule can't be relied on once a per-widget
    stylesheet is set (see button_style()'s docstring -- same reasoning,
    applied to :checked instead of :hover/:pressed)."""
    return f"""
        QPushButton {{
            background-color: {colors['surface']};
            color: {colors['text']};
            border: 2px solid {colors['border']};
            border-radius: {radius}px;
            font-size: {size}px;
            font-weight: bold;
            padding: 4px;
        }}
        QPushButton:hover {{
            background-color: {colors['surface_hover']};
            border-color: {colors['accent']};
        }}
        QPushButton:checked {{
            background-color: {colors['accent']};
            color: {colors['bg']};
            border-color: {colors['accent']};
        }}
        QPushButton:pressed {{
            background-color: {colors['selection_bg']};
            color: {colors['selection_text']};
        }}
    """


def primary_button_style(colors, size=15, radius=6):
    """A filled, accent-colored variant for the one action on a screen
    that should read as the bigger/more deliberate choice (e.g.
    GameHostWindow's "Skip Break", a bigger decision than "Skip Game")."""
    return f"""
        QPushButton {{
            background-color: {colors['accent']};
            color: {colors['bg']};
            border: 2px solid {colors['accent']};
            border-radius: {radius}px;
            font-size: {size}px;
            font-weight: bold;
            padding: 4px 12px;
        }}
        QPushButton:hover {{
            background-color: {colors['selection_bg']};
            color: {colors['selection_text']};
        }}
        QPushButton:pressed {{
            background-color: {colors['text_dim']};
        }}
        QPushButton:disabled {{
            background-color: {colors['surface']};
            color: {colors['text_dim']};
            border-color: {colors['text_dim']};
        }}
    """

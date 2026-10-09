import support  # noqa: F401  (must stay first: it disables bytecode and isolates the environment)

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from omawrapped import system
from support import PLUGIN_ID, IsolatedCase

DEFAULTS = system.Theme()


class ThemeTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.state_current = self.home / ".local" / "state" / "omarchy" / "current"
        self.config_current = self.home / ".config" / "omarchy" / "current"

    def colors(self, folder: Path, text: str) -> Path:
        return self.write(folder / "colors.toml", text)

    def test_reads_colors_and_name_from_the_state_folder(self):
        self.colors(self.state_current / "theme",
                    'background = "#112233"\nforeground = "#ddeeff"\naccent = "#ff8800"\n')
        self.write(self.state_current / "theme.name", "matte-black\n")
        found = system.theme()
        self.assertEqual((found.background, found.foreground, found.accent), ("#112233", "#ddeeff", "#ff8800"))
        self.assertEqual(found.name, "Matte Black")

    def test_falls_back_to_the_config_folder(self):
        self.colors(self.config_current / "theme",
                    'background = "#010203"\nforeground = "#f0f0f0"\naccent = "#00ff00"\n')
        found = system.theme()
        self.assertEqual((found.background, found.foreground, found.accent), ("#010203", "#f0f0f0", "#00ff00"))

    def test_the_config_folder_names_the_theme_from_the_file_beside_it(self):
        self.colors(self.config_current / "theme", 'background = "#010203"\n')
        self.write(self.config_current / "theme.name", "rose-pine\n")
        self.assertEqual(system.theme().name, "Rose Pine")

    def test_a_linked_theme_folder_is_named_after_its_target(self):
        # Before theme.name existed, `theme` was a link to a folder named after the theme.
        target = self.tmp / "themes" / "osaka-jade"
        self.colors(target, 'background = "#010203"\n')
        self.config_current.mkdir(parents=True)
        (self.config_current / "theme").symlink_to(target)
        found = system.theme()
        self.assertEqual(found.background, "#010203")
        self.assertEqual(found.name, "Osaka Jade")

    def test_the_state_folder_wins_over_the_config_folder(self):
        self.colors(self.state_current / "theme", 'background = "#111111"\n')
        self.colors(self.config_current / "theme", 'background = "#222222"\n')
        self.assertEqual(system.theme().background, "#111111")

    def test_with_no_theme_at_all_the_default_colors_are_returned(self):
        found = system.theme()
        self.assertEqual(found, DEFAULTS)
        self.assertEqual(found.name, "")
        for color in (found.background, found.foreground, found.accent):
            self.assertRegex(color, r"^#[0-9a-f]{6}$")

    def test_a_given_folder_is_read_and_names_the_theme(self):
        folder = self.tmp / "themes" / "tokyo-night"
        self.colors(folder, 'background = "#1a1b26"\nforeground = "#c0caf5"\naccent = "#7aa2f7"\n')
        # The active theme must play no part when a folder is given.
        self.colors(self.state_current / "theme", 'background = "#ffffff"\n')
        self.write(self.state_current / "theme.name", "other-theme")
        found = system.theme(folder)
        self.assertEqual((found.background, found.foreground, found.accent), ("#1a1b26", "#c0caf5", "#7aa2f7"))
        self.assertEqual(found.name, "Tokyo Night")

    def test_a_given_folder_may_be_a_string(self):
        folder = self.tmp / "themes" / "nord"
        self.colors(folder, 'background = "#2e3440"\n')
        self.assertEqual(system.theme(str(folder)).background, "#2e3440")

    def test_a_given_folder_without_colors_gives_the_defaults_under_its_name(self):
        folder = self.tmp / "themes" / "empty-one"
        folder.mkdir(parents=True)
        found = system.theme(folder)
        self.assertEqual((found.background, found.foreground, found.accent), (
            DEFAULTS.background, DEFAULTS.foreground, DEFAULTS.accent))
        self.assertEqual(found.name, "Empty One")

    def test_missing_named_keys_fall_back_to_color0_color7_and_color4(self):
        folder = self.tmp / "themes" / "ansi"
        self.colors(folder, 'color0 = "#101010"\ncolor7 = "#e0e0e0"\ncolor4 = "#3366cc"\n')
        found = system.theme(folder)
        self.assertEqual((found.background, found.foreground, found.accent), ("#101010", "#e0e0e0", "#3366cc"))

    def test_named_keys_win_over_the_ansi_ones(self):
        folder = self.tmp / "themes" / "both"
        self.colors(folder, 'background = "#111111"\ncolor0 = "#222222"\nforeground = "#eeeeee"\ncolor7 = "#dddddd"\n'
                            'accent = "#ff0000"\ncolor4 = "#0000ff"\n')
        found = system.theme(folder)
        self.assertEqual((found.background, found.foreground, found.accent), ("#111111", "#eeeeee", "#ff0000"))

    def test_without_an_accent_the_foreground_is_used(self):
        folder = self.tmp / "themes" / "plain"
        self.colors(folder, 'background = "#101010"\nforeground = "#e0e0e0"\n')
        self.assertEqual(system.theme(folder).accent, "#e0e0e0")

    def test_values_that_are_not_rrggbb_are_ignored(self):
        for bad in ('"red"', '"#fff"', '"#12345"', '"#1234567"', '"112233"', '"#gggggg"', '"# 12345"', "123456",
                    "true", '""', "[1, 2, 3]"):
            with self.subTest(value=bad):
                folder = self.tmp / "themes" / "bad"
                self.colors(folder, "background = %s\nforeground = %s\naccent = %s\n" % (bad, bad, bad))
                found = system.theme(folder)
                self.assertEqual((found.background, found.foreground, found.accent), (
                    DEFAULTS.background, DEFAULTS.foreground, DEFAULTS.foreground))

    def test_a_bad_value_gives_way_to_the_ansi_key(self):
        folder = self.tmp / "themes" / "mixed"
        self.colors(folder, 'background = "dark"\ncolor0 = "#0a0b0c"\naccent = "#xyz"\ncolor4 = "#0c0b0a"\n')
        found = system.theme(folder)
        self.assertEqual((found.background, found.accent), ("#0a0b0c", "#0c0b0a"))

    def test_a_colour_followed_by_a_line_break_is_not_a_colour(self):
        # The value goes into an SVG attribute as it stands.
        self.colors(self.state_current / "theme", 'background = "#112233\\n"\nforeground = "#ddeeff"\n')
        found = system.theme()
        self.assertEqual(found.background, DEFAULTS.background)
        self.assertEqual(found.foreground, "#ddeeff")

    def test_hex_digits_may_be_upper_case(self):
        folder = self.tmp / "themes" / "caps"
        self.colors(folder, 'background = "#AABBCC"\n')
        self.assertEqual(system.theme(folder).background.lower(), "#aabbcc")

    def test_a_broken_toml_file_is_ignored(self):
        folder = self.tmp / "themes" / "broken"
        self.colors(folder, 'background = "#112233\nthis is = = not toml [[[')
        found = system.theme(folder)
        self.assertEqual((found.background, found.foreground, found.accent), (
            DEFAULTS.background, DEFAULTS.foreground, DEFAULTS.accent))

    def test_a_file_that_is_not_utf8_is_ignored(self):
        folder = self.tmp / "themes" / "binary"
        folder.mkdir(parents=True)
        (folder / "colors.toml").write_bytes(b'background = "\xff\xfe"\n')
        self.assertEqual(system.theme(folder).background, DEFAULTS.background)

    def test_a_broken_active_theme_gives_way_to_the_config_folder(self):
        self.colors(self.state_current / "theme", "not = [valid")
        self.colors(self.config_current / "theme", 'background = "#abcdef"\n')
        self.assertEqual(system.theme().background, "#abcdef")

    def test_a_broken_active_theme_alone_gives_the_defaults(self):
        self.colors(self.state_current / "theme", "not = [valid")
        self.assertEqual(system.theme(), DEFAULTS)

    def test_theme_names_are_title_cased_from_their_slugs(self):
        for slug, title in (("matte-black", "Matte Black"), ("tokyo-night", "Tokyo Night"), ("nord", "Nord"),
                            ("rose-pine", "Rose Pine"), ("a--b", "A B"), ("-x-", "X")):
            with self.subTest(slug=slug):
                self.write(self.state_current / "theme.name", slug + "\n")
                self.assertEqual(system.theme().name, title)

    def test_an_empty_name_file_gives_no_name(self):
        self.write(self.state_current / "theme.name", "\n")
        self.assertEqual(system.theme().name, "")


class PluginCountTests(IsolatedCase):
    def test_counts_only_folders_with_a_manifest(self):
        plugins = self.home / ".config" / "omarchy" / "plugins"
        self.write(plugins / "alpha" / "manifest.json", "{}")
        self.write(plugins / "beta" / "manifest.json", "{}")
        self.write(plugins / "no-manifest" / "README.md", "x")
        (plugins / "manifest-is-a-folder" / "manifest.json").mkdir(parents=True)
        self.write(plugins / "stray-file.txt", "x")
        self.write(plugins / "manifest.json", "{}")
        self.assertEqual(system.plugin_count(), 2)

    def test_no_plugins_folder_means_zero(self):
        self.assertEqual(system.plugin_count(), 0)

    def test_an_empty_plugins_folder_means_zero(self):
        (self.home / ".config" / "omarchy" / "plugins").mkdir(parents=True)
        self.assertEqual(system.plugin_count(), 0)


class OmarchyVersionTests(IsolatedCase):
    def test_reads_the_version_file(self):
        self.write(self.omarchy / "version", "3.5.2\n")
        self.assertEqual(system.omarchy_version(), "3.5.2")

    def test_missing_file_gives_an_empty_string(self):
        self.assertEqual(system.omarchy_version(), "")

    def test_empty_file_gives_an_empty_string(self):
        self.write(self.omarchy / "version", "")
        self.assertEqual(system.omarchy_version(), "")
        self.write(self.omarchy / "version", "\n\n")
        self.assertEqual(system.omarchy_version(), "")

    def test_only_the_first_line_is_used(self):
        self.write(self.omarchy / "version", "3.5.2\nsecond line\n")
        self.assertEqual(system.omarchy_version(), "3.5.2")

    def test_a_long_line_is_cut(self):
        self.write(self.omarchy / "version", "v" * 100)
        self.assertLessEqual(len(system.omarchy_version()), 24)
        self.assertTrue(system.omarchy_version().startswith("vvvv"))


class SettingsTests(IsolatedCase):
    def write_config(self, config) -> Path:
        """shell.json holding `config` as JSON."""
        return self.write_raw(json.dumps(config))

    def write_raw(self, text: str) -> Path:
        return self.write(self.home / ".config" / "omarchy" / "shell.json", text)

    def layout(self, **sections) -> dict:
        return {"bar": {"layout": sections}}

    def test_finds_the_entry_in_each_of_the_three_sections(self):
        entry = {"id": PLUGIN_ID, "repoDirs": "~/src, ~/work", "idleSeconds": 90}
        for section in ("left", "center", "right"):
            with self.subTest(section=section):
                other = {"id": "someone.else"}
                self.write_config(self.layout(**{section: [other, "stray string", 7, entry]}))
                self.assertEqual(system.settings(), entry)

    def test_other_plugins_do_not_match(self):
        self.write_config(self.layout(left=[{"id": "other.plugin", "x": 1}], right=[{"id": PLUGIN_ID + ".more"}]))
        self.assertEqual(system.settings(), {})

    def test_a_missing_file_gives_an_empty_dict(self):
        self.assertEqual(system.settings(), {})

    def test_invalid_json_gives_an_empty_dict(self):
        for text in ("", "{", "not json", '{"bar": '):
            with self.subTest(text=text):
                self.write_raw(text)
                self.assertEqual(system.settings(), {})

    def test_layouts_of_the_wrong_shape_give_an_empty_dict(self):
        entry = {"id": PLUGIN_ID}
        shapes = [
            [entry],                                      # the whole file is a list
            None,
            "text",
            {},                                           # no bar
            {"bar": []},
            {"bar": None},
            {"bar": "text"},
            {"bar": {}},                                  # no layout
            {"bar": {"layout": [entry]}},                 # layout is a list
            {"bar": {"layout": "text"}},
            {"bar": {"layout": None}},
            {"bar": {"layout": {"left": entry}}},         # a section that is not a list
            {"bar": {"layout": {"left": "text", "right": None}}},
            {"layout": {"left": [entry]}},                # layout not under bar
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                self.write_config(shape)
                self.assertEqual(system.settings(), {})

    def test_a_section_of_the_wrong_shape_does_not_hide_a_good_one(self):
        entry = {"id": PLUGIN_ID, "x": 1}
        self.write_config(self.layout(left="text", center=None, right=[entry]))
        self.assertEqual(system.settings(), entry)


class SplitListTests(unittest.TestCase):
    def test_comma_separated_text(self):
        self.assertEqual(system.split_list("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(system.split_list("~/projects, ~/code"), ["~/projects", "~/code"])

    def test_empty_parts_are_dropped(self):
        self.assertEqual(system.split_list(",, a ,, ,b,"), ["a", "b"])

    def test_a_list_is_taken_as_it_is_with_each_item_trimmed(self):
        self.assertEqual(system.split_list([" a ", "b", "", "  "]), ["a", "b"])
        self.assertEqual(system.split_list([1, 2]), ["1", "2"])

    def test_nothing_gives_an_empty_list(self):
        for value in (None, "", "   ", ",", [], 0, False):
            with self.subTest(value=value):
                self.assertEqual(system.split_list(value), [])

    def test_a_single_value(self):
        self.assertEqual(system.split_list("~/code"), ["~/code"])


class PicturesDirTests(IsolatedCase):
    def test_xdg_pictures_dir_wins(self):
        elsewhere = self.tmp / "elsewhere"
        self.write(self.config / "user-dirs.dirs", 'XDG_PICTURES_DIR="$HOME/Bilder"\n')
        with mock.patch.dict(os.environ, {"XDG_PICTURES_DIR": str(elsewhere)}):
            self.assertEqual(system.pictures_dir(), elsewhere)

    def test_user_dirs_file_with_home_in_it(self):
        config = self.tmp / "custom-config"
        self.write(config / "user-dirs.dirs",
                   '# This file is written by xdg-user-dirs-update\nXDG_DESKTOP_DIR="$HOME/Schreibtisch"\n'
                   'XDG_PICTURES_DIR="$HOME/Bilder"\nXDG_MUSIC_DIR="$HOME/Musik"\n')
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            del os.environ["XDG_PICTURES_DIR"]
            self.assertEqual(system.pictures_dir(), self.home / "Bilder")

    def test_user_dirs_file_with_an_absolute_path(self):
        self.write(self.config / "user-dirs.dirs", 'XDG_DESKTOP_DIR="$HOME/Desktop"\nXDG_PICTURES_DIR="/mnt/photos"\n')
        with mock.patch.dict(os.environ):
            del os.environ["XDG_PICTURES_DIR"]
            self.assertEqual(system.pictures_dir(), Path("/mnt/photos"))

    def test_user_dirs_file_is_found_in_the_default_config_folder(self):
        self.write(self.home / ".config" / "user-dirs.dirs", 'XDG_PICTURES_DIR="$HOME/Fotos"\n')
        with mock.patch.dict(os.environ):
            del os.environ["XDG_PICTURES_DIR"]
            del os.environ["XDG_CONFIG_HOME"]
            self.assertEqual(system.pictures_dir(), self.home / "Fotos")

    def test_a_commented_out_line_is_not_used(self):
        self.write(self.config / "user-dirs.dirs", '# XDG_PICTURES_DIR="$HOME/Nope"\n')
        with mock.patch.dict(os.environ):
            del os.environ["XDG_PICTURES_DIR"]
            self.assertEqual(system.pictures_dir(), self.home / "Pictures")

    def test_neither_gives_pictures_in_home(self):
        with mock.patch.dict(os.environ):
            del os.environ["XDG_PICTURES_DIR"]
            self.assertEqual(system.pictures_dir(), self.home / "Pictures")

    def test_an_empty_setting_counts_as_unset(self):
        with mock.patch.dict(os.environ, {"XDG_PICTURES_DIR": ""}):
            self.assertEqual(system.pictures_dir(), self.home / "Pictures")

    def test_a_relative_setting_is_not_used(self):
        with mock.patch.dict(os.environ, {"XDG_PICTURES_DIR": "relative/pics"}):
            self.assertEqual(system.pictures_dir(), self.home / "Pictures")


class MonospaceFamilyTests(IsolatedCase):
    """fc-match is never run for real here: its answer is made up."""

    def family_for(self, stdout="", returncode=0, error=None) -> str:
        done = subprocess.CompletedProcess(["fc-match"], returncode, stdout=stdout, stderr="")
        with mock.patch.object(system.subprocess, "run", return_value=done, side_effect=error) as run:
            family = system.monospace_family()
        self.assertEqual(run.call_args.args[0][0], "fc-match")
        return family

    def test_the_family_fc_match_reports_is_used(self):
        self.assertEqual(self.family_for("JetBrainsMono Nerd Font\n"), "JetBrainsMono Nerd Font")
        self.assertEqual(self.family_for("DejaVu Sans Mono"), "DejaVu Sans Mono")

    def test_a_failing_or_missing_fc_match_falls_back_to_the_alias(self):
        self.assertEqual(self.family_for("Something", returncode=1), "monospace")
        self.assertEqual(self.family_for(error=FileNotFoundError()), "monospace")
        self.assertEqual(self.family_for(error=subprocess.TimeoutExpired("fc-match", 5)), "monospace")

    def test_an_unusual_name_falls_back_because_it_goes_into_an_svg_attribute(self):
        for family in ("", 'Evil"><script/>', "Foo'Bar", "a<b", "x" * 81, "Fo\to"):
            with self.subTest(family=family):
                self.assertEqual(self.family_for(family), "monospace")


def desktop(name, extra="", header="[Desktop Entry]") -> str:
    return "%s\nType=Application\nName=%s\n%s\n" % (header, name, extra)


class AppKeyTests(unittest.TestCase):
    """app_key must agree with appKey in Tracker.js, which decides what is stored."""

    def test_a_web_app_is_keyed_by_its_site_alone(self):
        self.assertEqual(system.app_key("chrome-web.whatsapp.com__-Default"), "web:web.whatsapp.com")
        self.assertEqual(system.app_key("chrome-discord.com__channels_@me-Default"), "web:discord.com")
        self.assertEqual(system.app_key("brave-music.youtube.com__watch-Profile_1"), "web:music.youtube.com")
        self.assertEqual(system.app_key("chrome-my-team.Example.NET__-Default"), "web:my-team.example.net")
        self.assertEqual(system.app_key("chrome-docs.example.com__document_d_1AbCdEf_edit-Private_Profile"),
                         "web:docs.example.com")

    def test_anything_else_is_its_own_key(self):
        for app in ("chromium", "com.mitchellh.ghostty", "org.gnome.Nautilus", "steam_app_12345", "Google-chrome",
                    "foo-bar__baz-qux", "chrome-localhost__-Default", "web:x.com", "Ghostty", "Visual Studio Code"):
            with self.subTest(app=app):
                self.assertEqual(system.app_key(app), app)
        self.assertEqual(system.app_key("  slack \n"), "slack")


class AppNamesTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.apps = self.tmp / "applications"

    def names(self, *folders) -> system.AppNames:
        return system.AppNames(list(folders) if folders else [self.apps])

    def test_startup_wm_class_match(self):
        self.write(self.apps / "foo.desktop", desktop("Foo App", "StartupWMClass=FooWindow"))
        self.assertEqual(self.names().name("FooWindow"), "Foo App")

    def test_startup_wm_class_match_ignores_case(self):
        self.write(self.apps / "foo.desktop", desktop("Foo App", "StartupWMClass=FooWindow"))
        names = self.names()
        self.assertEqual(names.name("foowindow"), "Foo App")
        self.assertEqual(names.name("FOOWINDOW"), "Foo App")

    def test_desktop_file_id_match(self):
        self.write(self.apps / "org.gnome.Nautilus.desktop", desktop("Files"))
        self.assertEqual(self.names().name("org.gnome.Nautilus"), "Files")

    def test_desktop_file_id_match_ignores_case(self):
        self.write(self.apps / "org.gnome.Nautilus.desktop", desktop("Files"))
        names = self.names()
        self.assertEqual(names.name("org.gnome.nautilus"), "Files")
        self.assertEqual(names.name("ORG.GNOME.NAUTILUS"), "Files")

    def test_the_names_are_trimmed(self):
        self.write(self.apps / "padded.desktop",
                   "[Desktop Entry]\nName=  Padded Name  \nStartupWMClass= padded-class \n")
        self.assertEqual(self.names().name("padded-class"), "Padded Name")

    def test_hidden_entries_are_skipped(self):
        self.write(self.apps / "secret.desktop", desktop("Secret Thing", "NoDisplay=true\nStartupWMClass=secretwin"))
        self.write(self.apps / "shout.desktop", desktop("Shouted Thing", "NoDisplay=True\nStartupWMClass=shoutwin"))
        names = self.names()
        self.assertEqual(names.name("secretwin"), "Secretwin")
        self.assertEqual(names.name("secret"), "Secret")
        self.assertEqual(names.name("shoutwin"), "Shoutwin")

    def test_an_entry_that_says_nodisplay_false_is_used(self):
        self.write(self.apps / "shown.desktop", desktop("Shown Thing", "NoDisplay=false\nStartupWMClass=shownwin"))
        self.assertEqual(self.names().name("shownwin"), "Shown Thing")

    def test_an_entry_without_a_name_is_skipped(self):
        self.write(self.apps / "nameless.desktop", "[Desktop Entry]\nType=Application\nStartupWMClass=nameless-win\n")
        self.assertEqual(self.names().name("nameless-win"), "Nameless win")

    def test_the_first_folder_wins_over_a_later_one(self):
        first, second = self.tmp / "first", self.tmp / "second"
        self.write(first / "app.desktop", desktop("From First", "StartupWMClass=appwin"))
        self.write(second / "app.desktop", desktop("From Second", "StartupWMClass=appwin"))
        self.write(second / "only-second.desktop", desktop("Only Second"))
        names = self.names(first, second)
        self.assertEqual(names.name("app"), "From First")
        self.assertEqual(names.name("appwin"), "From First")
        self.assertEqual(names.name("only-second"), "Only Second")
        self.assertEqual(self.names(second, first).name("app"), "From Second")

    def test_an_omarchy_web_app_resolves_its_chromium_class(self):
        self.write(self.apps / "WhatsApp.desktop",
                   desktop("WhatsApp", "Exec=omarchy-launch-webapp https://web.whatsapp.com/\nIcon=WhatsApp"))
        self.assertEqual(self.names().name("chrome-web.whatsapp.com__-Default"), "WhatsApp")

    def test_a_chromium_app_flag_entry_resolves_its_class(self):
        self.write(self.apps / "yt.desktop",
                   desktop("YouTube", 'Exec=chromium --app="https://www.youtube.com/" --class=x'))
        names = self.names()
        self.assertEqual(names.name("chrome-www.youtube.com__-Default"), "YouTube")
        self.assertEqual(names.name("chrome-youtube.com__-Default"), "YouTube")

    def test_a_web_app_without_an_entry_falls_back_to_its_host(self):
        names = self.names()
        self.assertEqual(names.name("chrome-www.example.com__-Default"), "example.com")
        self.assertEqual(names.name("chrome-web.example.com__-Default"), "web.example.com")
        self.assertEqual(names.name("chrome-app.slack.com__client_T123-Default"), "app.slack.com")
        self.assertEqual(names.name("chrome-Example.ORG__-Default"), "example.org")

    def test_the_key_the_sampler_stores_resolves_like_the_window_class(self):
        # The sampler stores a web app as web:<host>; older and hand-made data may hold the whole class.
        self.write(self.apps / "WhatsApp.desktop",
                   "[Desktop Entry]\nName=WhatsApp\nExec=omarchy-launch-webapp https://web.whatsapp.com/\n")
        names = self.names()
        self.assertEqual(names.name("web:web.whatsapp.com"), "WhatsApp")
        self.assertEqual(names.name("chrome-web.whatsapp.com__-Default"), "WhatsApp")
        self.assertEqual(names.name("web:www.example.com"), "example.com")
        self.assertEqual(names.name("web:intranet.example.com"), "intranet.example.com")

    def test_a_web_app_entry_for_another_site_does_not_match(self):
        self.write(self.apps / "WhatsApp.desktop",
                   desktop("WhatsApp", "Exec=omarchy-launch-webapp https://web.whatsapp.com/"))
        self.assertEqual(self.names().name("chrome-web.telegram.org__-Default"), "web.telegram.org")

    def test_reverse_dns_ids_become_the_last_part(self):
        names = self.names()
        self.assertEqual(names.name("com.mitchellh.ghostty"), "Ghostty")
        self.assertEqual(names.name("org.gnome.Nautilus"), "Nautilus")
        self.assertEqual(names.name("dev.zed.Zed"), "Zed")

    def test_plain_ids_are_tidied_up(self):
        names = self.names()
        self.assertEqual(names.name("slack"), "Slack")
        self.assertEqual(names.name("steam_app_12345"), "Steam app 12345")
        self.assertEqual(names.name("my-cool-app"), "My cool app")
        self.assertEqual(names.name("Chromium"), "Chromium")

    def test_an_entry_beats_the_tidied_id(self):
        self.write(self.apps / "dev.zed.Zed.desktop", desktop("Zed Editor"))
        self.assertEqual(self.names().name("dev.zed.Zed"), "Zed Editor")

    def test_an_id_made_only_of_separators_comes_back_as_it_is(self):
        self.assertEqual(self.names().name("--"), "--")
        self.assertEqual(self.names().name(""), "")

    def test_malformed_desktop_files_are_skipped_without_an_exception(self):
        self.write(self.apps / "no-header.desktop", "Name=Headless\nStartupWMClass=headlesswin\n")
        self.write(self.apps / "other-section.desktop", "[Desktop Action x]\nName=Elsewhere\n")
        self.write(self.apps / "garbage.desktop", "[[[[ = = =\n\x00\x01 ]]\n")
        self.write(self.apps / "empty.desktop", "")
        (self.apps / "not-utf8.desktop").write_bytes(b"[Desktop Entry]\nName=Bad \xff\xfe bytes\n")
        (self.apps / "a-folder.desktop").mkdir()
        self.write(self.apps / "good.desktop", desktop("Good App", "StartupWMClass=goodwin"))
        names = self.names()
        self.assertEqual(names.name("goodwin"), "Good App")
        self.assertEqual(names.name("headlesswin"), "Headlesswin")
        self.assertEqual(names.name("no-header"), "No header")

    def test_a_desktop_entry_with_percent_signs_and_translated_names_is_read(self):
        self.write(self.apps / "odd.desktop",
                   "# comment\n[Desktop Entry]\nName=Odd 100% App\nName[de]=Seltsam\n"
                   "StartupWMClass=oddwin\nExec=odd %U\n")
        self.assertEqual(self.names().name("oddwin"), "Odd 100% App")

    def test_missing_folders_are_fine(self):
        self.assertEqual(system.AppNames([self.tmp / "nowhere"]).name("slack"), "Slack")
        self.assertEqual(system.AppNames([]).name("slack"), "Slack")

    def test_only_desktop_files_are_read(self):
        self.write(self.apps / "readme.txt", "[Desktop Entry]\nName=Text File\nStartupWMClass=textwin\n")
        self.assertEqual(self.names().name("textwin"), "Textwin")

    def test_the_default_folders_come_from_the_environment_home_first(self):
        share_a, share_b = self.tmp / "share-a", self.tmp / "share-b"
        self.write(self.data_home / "applications" / "app.desktop", desktop("From Data Home", "StartupWMClass=thewin"))
        self.write(share_a / "applications" / "app.desktop", desktop("From A", "StartupWMClass=thewin"))
        self.write(share_b / "applications" / "app.desktop", desktop("From B", "StartupWMClass=thewin"))
        self.write(share_b / "applications" / "late.desktop", desktop("Late One"))
        self.write(share_a / "applications" / "early.desktop", desktop("Early One"))
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": "%s:%s" % (share_a, share_b)}):
            names = system.AppNames()
        self.assertEqual(names.name("thewin"), "From Data Home")
        self.assertEqual(names.name("early"), "Early One")
        self.assertEqual(names.name("late"), "Late One")

    def test_default_folders_that_hold_nothing_leave_only_the_tidied_id(self):
        # With the environment pointing nowhere that exists, nothing but the id itself is known.
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": str(self.tmp / "none-1"),
                                          "XDG_DATA_HOME": str(self.tmp / "none-2")}):
            self.assertEqual(system.AppNames().name("org.gnome.Nautilus"), "Nautilus")

    def test_answers_are_stable_when_asked_again(self):
        self.write(self.apps / "foo.desktop", desktop("Foo App", "StartupWMClass=foowin"))
        names = self.names()
        self.assertEqual([names.name("foowin"), names.name("foowin"), names.name("slack"), names.name("slack")],
                         ["Foo App", "Foo App", "Slack", "Slack"])


if __name__ == "__main__":
    unittest.main()

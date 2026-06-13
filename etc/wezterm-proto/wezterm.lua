-- Native WezTerm dashboard — runnable slice (Plan B2, phase 2).
-- Point WezTerm at this file:  WEZTERM_CONFIG_FILE=.../proto/wezterm.lua wezterm
-- or symlink to ~/.config/wezterm/wezterm.lua. Keybindings shell out to wzmux.py,
-- which spawns sessions into the same mux this GUI is attached to.
local wezterm = require 'wezterm'
local act = wezterm.action
local config = wezterm.config_builder()

local SCRIPT = wezterm.config_dir .. '/wzmux.py'
local PYTHON = '/opt/homebrew/bin/python3'

-- Native GPU renderer + the dashboard look.
config.front_end = 'WebGpu'
config.font = wezterm.font 'JetBrains Mono'
config.font_size = 13.0
config.use_fancy_tab_bar = true
config.hide_tab_bar_if_only_one_tab = false

-- Scroll: deep native scrollback + smooth wheel. Egde to feel during snappiness tests.
config.scrollback_lines = 50000
config.enable_scroll_bar = true
config.animation_fps = 60
config.max_fps = 120                 -- let the GPU push frames; this is the "snappy" knob
config.pane_focus_follows_mouse = true

-- Launching WezTerm attaches straight to the headless mux (the session backend),
-- so existing sessions show up immediately as tabs.
config.default_gui_startup_args = { 'connect', 'unix' }

local function pane_cwd(pane)
  local d = pane:get_current_working_dir()
  if d == nil then return wezterm.home_dir end
  if type(d) == 'userdata' then return d.file_path or wezterm.home_dir end
  return (tostring(d):gsub('^file://[^/]*', ''))  -- older wezterm returns a string
end

local function spawn(kind, cwd)
  wezterm.run_child_process({ PYTHON, SCRIPT, 'new', '--kind', kind, '--cwd', cwd })
end

-- New session of <kind> in the active pane's cwd.
wezterm.on('vibe-new-terminal', function(_, pane) spawn('terminal', pane_cwd(pane)) end)
wezterm.on('vibe-new-claude',   function(_, pane) spawn('claude',   pane_cwd(pane)) end)
wezterm.on('vibe-new-opencode', function(_, pane) spawn('opencode', pane_cwd(pane)) end)

-- Launcher palette: pick a project, spawn a claude session there.
wezterm.on('vibe-launch', function(window, pane)
  local ok, stdout = wezterm.run_child_process({ PYTHON, SCRIPT, 'projects' })
  local choices = {}
  if ok then
    for _, p in ipairs(wezterm.json_parse(stdout)) do
      choices[#choices + 1] = { label = p.label, id = p.cwd }
    end
  end
  window:perform_action(act.InputSelector {
    title = 'New claude session in…',
    choices = choices,
    fuzzy = true,
    action = wezterm.action_callback(function(_, _, id)
      if id then spawn('claude', id) end
    end),
  }, pane)
end)

config.keys = {
  { key = 't', mods = 'CMD',       action = act.EmitEvent 'vibe-new-terminal' },
  { key = 'c', mods = 'CMD|SHIFT', action = act.EmitEvent 'vibe-new-claude' },
  { key = 'o', mods = 'CMD|SHIFT', action = act.EmitEvent 'vibe-new-opencode' },
  { key = 'p', mods = 'CMD',       action = act.EmitEvent 'vibe-launch' },
  -- Native search replaces the custom xterm search addon.
  { key = 'f', mods = 'CMD',       action = act.Search 'CurrentSelectionOrEmptyString' },
  { key = 'w', mods = 'CMD',       action = act.CloseCurrentPane { confirm = true } },
  -- Native tab switching = the snappy part.
  { key = '[', mods = 'CMD|SHIFT', action = act.ActivateTabRelative(-1) },
  { key = ']', mods = 'CMD|SHIFT', action = act.ActivateTabRelative(1) },
  -- Tiling: split the active pane (the native grid).
  { key = 'd', mods = 'CMD',       action = act.SplitHorizontal { domain = 'CurrentPaneDomain' } }, -- side-by-side
  { key = 'd', mods = 'CMD|SHIFT', action = act.SplitVertical   { domain = 'CurrentPaneDomain' } }, -- stacked
  -- Move focus between tiles.
  { key = 'LeftArrow',  mods = 'CMD|ALT', action = act.ActivatePaneDirection 'Left' },
  { key = 'RightArrow', mods = 'CMD|ALT', action = act.ActivatePaneDirection 'Right' },
  { key = 'UpArrow',    mods = 'CMD|ALT', action = act.ActivatePaneDirection 'Up' },
  { key = 'DownArrow',  mods = 'CMD|ALT', action = act.ActivatePaneDirection 'Down' },
  -- Scroll the active pane.
  { key = 'UpArrow',   mods = 'CMD',       action = act.ScrollByLine(-3) },
  { key = 'DownArrow', mods = 'CMD',       action = act.ScrollByLine(3) },
  { key = 'PageUp',    mods = 'SHIFT',     action = act.ScrollByPage(-1) },
  { key = 'PageDown',  mods = 'SHIFT',     action = act.ScrollByPage(1) },
}

return config

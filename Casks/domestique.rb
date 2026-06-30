# Homebrew Cask formula for Domestique.
#
# This file is the canonical Cask source. To distribute via Homebrew, copy
# this into a separate tap repository (e.g. `platypus45/homebrew-tap` →
# `Casks/domestique.rb`) and bump `version` + `sha256` on every release.
#
# Users install via:
#
#     brew tap platypus45/tap
#     brew install --cask domestique
#
# Homebrew Cask auto-strips the `com.apple.quarantine` extended attribute
# on install, so the "unidentified developer" Gatekeeper dialog is bypassed
# entirely — users don't need the right-click → Open dance.
#
# Bumping for a new release:
#
#     VER=1.8.4
#     curl -sL https://github.com/platypus45/domestique/releases/download/v${VER}/Domestique-v${VER}.dmg -o /tmp/D.dmg
#     shasum -a 256 /tmp/D.dmg
#     # Update version + sha256 below, then commit + push to the tap repo.
cask "domestique" do
  version "2.4.1"
  sha256 "743991413afb5b103c37a6e13a7ba7aca2ba0dc6f85b9dc2c009380cc892918b"

  url "https://github.com/platypus45/domestique/releases/download/v#{version}/Domestique-v#{version}.dmg"
  name "Domestique"
  desc "Adaptive cycling training planner — closed-loop ZWO + FIT pipeline"
  homepage "https://github.com/platypus45/domestique"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on macos: ">= :big_sur"

  app "Domestique.app"

  zap trash: [
    "~/.domestique",
    "~/Library/Preferences/dev.domestique.*.plist",
    "~/Library/Saved Application State/dev.domestique.*.savedState",
  ]
end

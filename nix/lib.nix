# nix/lib.nix — Shared helpers for nix stuff
#
# All npm packages in this repo are workspace members sharing a single
# root package-lock.json.  mkNpmPassthru provides the shared src, npmDeps,
# npmRoot, and npmDepsFetcherVersion so individual .nix files don't
# duplicate them.  One hash to rule them all.
{
  pkgs,
  npm-lockfile-fix,
  nodejs,
}:
let
  # The workspace root — where the single package-lock.json lives.
  src = ../.;

  # Single npm deps fetch from the workspace root lockfile.
  # All workspace packages share this derivation.
  npmDepsHash = "sha256-dJwiwxEuCJPTSKDx79sqGwQp5NkCQF5QGW5lBITGxoE=";

  npmDeps = pkgs.fetchNpmDeps {
    inherit src;
    fetcherVersion = 2;
    hash = npmDepsHash;
  };
in
{
  # Returns a buildNpmPackage-compatible attrs set that provides:
  #   src, npmDeps, npmRoot, npmDepsFetcherVersion
  #   patchPhase             — ensures root lockfile has exactly one trailing newline
  #   nativeBuildInputs      — [ updateLockfileScript ] (list, prepend with ++ for more)
  #   passthru.devShellHook  — stamp-checked npm install + hash auto-update
  #   passthru.npmLockfile   — metadata for mkFixLockfiles
  #   nodejs                 — fixed nodejs version for all packages we use in the repo
  #
  # NOTE: npmConfigHook runs `diff` between the source lockfile and the
  # npm-deps cache lockfile. fetchNpmDeps preserves whatever trailing
  # newlines the lockfile has. The patchPhase normalizes to exactly one
  # trailing newline so both sides always match.
  #
  # Usage:
  #   npm = hermesNpmLib.mkNpmPassthru { folder = "ui-tui"; attr = "tui"; pname = "hermes-tui"; };
  #   pkgs.buildNpmPackage (npm // {
  #     sourceRoot = "ui-tui";
  #     buildPhase = '' ... '';
  #     installPhase = '' ... '';
  #   })
  mkNpmPassthru =
    {
      folder, # repo-relative folder with package.json, e.g. "ui-tui"
      attr, # flake package attr, e.g. "tui"
      pname, # e.g. "hermes-tui"
      nixFile ? "nix/${attr}.nix", # defaults to nix/<attr>.nix
    }:
    let
      # No sourceRoot — the workspace root (with the single package-lock.json)
      # is auto-detected as sourceRoot by nix.  npmRoot stays at "."
      # so npmConfigHook finds the lockfile there.
    in
    {
      inherit src npmDeps nodejs;
      npmRoot = ".";
      npmDepsFetcherVersion = 2;

      # --ignore-scripts: the workspace includes electron (apps/desktop)
      # which has a postinstall that tries to download from github.com.
      # nix builds are offline, so all scripts must be skipped.  Each
      # package sets up its own build commands in buildPhase instead.
      npmFlags = [ "--ignore-scripts" ];

      patchPhase = ''
        runHook prePatch
        # Normalize trailing newlines on the root lockfile so source and
        # npm-deps always match, regardless of what fetchNpmDeps preserves.
        sed -i -z 's/\\n*$/\\n/' package-lock.json

        # Make npmConfigHook's byte-for-byte diff newline-agnostic by
        # replacing its hardcoded /nix/store/.../diff with a wrapper that
        # normalizes trailing newlines on both sides before comparing.
        mkdir -p "$TMPDIR/bin"
        cat > "$TMPDIR/bin/diff" << DIFFWRAP
        #!/bin/sh
        f1=\\$(mktemp) && sed -z 's/\\n*$/\\n/' "\\$1" > "\\$f1"
        f2=\\$(mktemp) && sed -z 's/\\n*$/\\n/' "\\$2" > "\\$f2"
        ${pkgs.diffutils}/bin/diff "\\$f1" "\\$f2" && rc=0 || rc=\\$?
        rm -f "\\$f1" "\\$f2"
        exit \\$rc
        DIFFWRAP
        chmod +x "$TMPDIR/bin/diff"
        export PATH="$TMPDIR/bin:$PATH"

        runHook postPatch
      '';

      nativeBuildInputs = [
        (pkgs.writeShellScriptBin "update_${attr}_lockfile" ''
          set -euox pipefail

          REPO_ROOT=$(git rev-parse --show-toplevel)

          # All workspace packages share the root lockfile.
          cd "$REPO_ROOT"
          rm -rf node_modules/
          ${pkgs.lib.getExe' nodejs "npm"} cache clean --force
          CI=true ${pkgs.lib.getExe' nodejs "npm"} install --workspaces
          ${pkgs.lib.getExe npm-lockfile-fix} ./package-lock.json

          NIX_FILE="$REPO_ROOT/${nixFile}"
          # No per-file hash anymore — the hash lives in lib.nix.
          # Just rebuild to verify.
          nix build .#${attr}
          echo "Lockfile updated and build verified for .#${attr}"
        '')
      ];

      passthru = {
        devShellHook = pkgs.writeShellScript "npm-dev-hook-${pname}" ''
          REPO_ROOT=$(git rev-parse --show-toplevel)

          # All workspace packages share the root package-lock.json.
          _hermes_npm_stamp() {
            sha256sum "${folder}/package.json" "package-lock.json" \
              2>/dev/null | sha256sum | awk '{print $1}'
          }
          STAMP=".nix-stamps/${pname}"
          STAMP_VALUE="$(_hermes_npm_stamp)"
          if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$STAMP_VALUE" ]; then
            echo "${pname}: installing npm dependencies..."
            ( cd "$REPO_ROOT" && CI=true ${pkgs.lib.getExe' nodejs "npm"} install --silent --no-fund --no-audit --workspaces 2>/dev/null )

            # Auto-update the nix hash so it stays in sync with the lockfile
            echo "${pname}: prefetching npm deps..."
            NIX_FILE="$REPO_ROOT/${nixFile}"
            if NEW_HASH=$(${pkgs.lib.getExe pkgs.prefetch-npm-deps} "package-lock.json" 2>/dev/null); then
              sed -i -E "s|npmDepsHash = \"sha256-[A-Za-z0-9+/=]+\";|npmDepsHash = \"$NEW_HASH\";|" "$REPO_ROOT/nix/lib.nix"
              echo "${pname}: updated hash to $NEW_HASH"
            else
              echo "${pname}: warning: prefetch failed, run 'nix run .#fix-lockfiles' manually" >&2
            fi

            mkdir -p .nix-stamps
            _hermes_npm_stamp > "$STAMP"
          fi
          unset -f _hermes_npm_stamp
        '';

        npmLockfile = {
          inherit attr folder nixFile;
        };
      };
    };

  # Aggregate `fix-lockfiles` bin from a list of packages carrying
  #   passthru.npmLockfile = { attr; folder; nixFile; };
  # Invocations:
  #   fix-lockfiles --check   # exit 1 if any hash is stale
  #   fix-lockfiles --apply   # rewrite stale hashes in place
  #   fix-lockfiles           # alias of --apply
  # Writes machine-readable fields (stale, changed, report) to $GITHUB_OUTPUT
  # when set, so CI workflows can post a sticky PR comment directly.
  mkFixLockfiles =
    {
      packages, # list of packages with passthru.npmLockfile
    }:
    let
      packagesWithLockfile = builtins.filter (p: p.passthru ? npmLockfile) packages;
      entries = map (p: p.passthru.npmLockfile) packagesWithLockfile;
      entryArgs = pkgs.lib.concatMapStringsSep " " (e: "\"${e.attr}:${e.folder}:${e.nixFile}\"") entries;
    in
    pkgs.writeShellScriptBin "fix-lockfiles" ''
      set -uox pipefail
      MODE="''${1:---apply}"
      case "$MODE" in
        --check|--apply) ;;
        -h|--help)
          echo "usage: fix-lockfiles [--check|--apply]"
          exit 0 ;;
        *)
          echo "usage: fix-lockfiles [--check|--apply]" >&2
          exit 2 ;;
      esac

      ENTRIES=(${entryArgs})

      REPO_ROOT="$(git rev-parse --show-toplevel)"
      cd "$REPO_ROOT"

      # When running in GH Actions, emit Markdown links in the report pointing
      # at the offending line of the nix file (and the lockfile) at the exact
      # commit that was checked. LINK_SHA should be set by the workflow to the
      # PR head SHA; falls back to GITHUB_SHA (which on pull_request is the
      # test-merge commit, still browseable).
      LINK_SERVER="''${GITHUB_SERVER_URL:-https://github.com}"
      LINK_REPO="''${GITHUB_REPOSITORY:-}"
      LINK_SHA="''${LINK_SHA:-''${GITHUB_SHA:-}}"

      STALE=0
      FIXED=0
      REPORT=""

      # All workspace packages share the root package-lock.json, so
      # we only need to check the hash once.
      LOCK_FILE="package-lock.json"
      LIB_FILE="nix/lib.nix"
      NEW_HASH=$(${pkgs.lib.getExe pkgs.prefetch-npm-deps} "$LOCK_FILE" 2>/dev/null)
      if [ -z "$NEW_HASH" ]; then
        echo "prefetch-npm-deps failed, falling back to nix build" >&2
        # Try any workspace package's npmDeps to get the hash
        FIRST_ATTR="''${ENTRIES[0]%%:*}"
        OUTPUT=$(nix build ".#''${FIRST_ATTR}.npmDeps" --no-link --print-build-logs 2>&1)
        STATUS=$?
        if [ "$STATUS" -eq 0 ]; then
          echo "ok (via nix build)"
          exit 0
        fi
        NEW_HASH=$(echo "$OUTPUT" | awk '/got:/ {print $2; exit}')
        if [ -z "$NEW_HASH" ]; then
          if echo "$OUTPUT" | grep -qE "throttled|HTTP error 418|substituter .* is disabled|some outputs of .* are not valid"; then
            echo "skipped (transient cache failure — see primary nix build for real status)" >&2
            echo "$OUTPUT" | tail -8 >&2
            exit 0
          fi
          echo "build failed with no hash mismatch:" >&2
          echo "$OUTPUT" | tail -40 >&2
          exit 1
        fi
      fi

      OLD_HASH=$(grep -oE 'npmDepsHash = "sha256-[^"]+"' "$LIB_FILE" | head -1 \
        | sed -E 's/npmDepsHash = "(.*)"/\1/')

      if [ "$NEW_HASH" = "$OLD_HASH" ]; then
        echo "ok"
        exit 0
      fi

      HASH_LINE=$(grep -n 'npmDepsHash = "sha256-' "$LIB_FILE" | head -1 | cut -d: -f1)
      echo "stale: $LIB_FILE:$HASH_LINE $OLD_HASH -> $NEW_HASH"
      STALE=1

      if [ -n "$LINK_REPO" ] && [ -n "$LINK_SHA" ]; then
        LIB_URL="$LINK_SERVER/$LINK_REPO/blob/$LINK_SHA/$LIB_FILE#L$HASH_LINE"
        LOCK_URL="$LINK_SERVER/$LINK_REPO/blob/$LINK_SHA/$LOCK_FILE"
        REPORT="- [\`$LIB_FILE:$HASH_LINE\`]($LIB_URL): \`$OLD_HASH\` → \`$NEW_HASH\` — lockfile: [\`$LOCK_FILE\`]($LOCK_URL)"$'\\n'
      else
        REPORT="- \`$LIB_FILE:$HASH_LINE\`: \`$OLD_HASH\` → \`$NEW_HASH\`"$'\\n'
      fi

      if [ "$MODE" = "--apply" ]; then
        sed -i -E "s|npmDepsHash = \"sha256-[^\"]+\";|npmDepsHash = \"$NEW_HASH\";|" "$LIB_FILE"
        if ! nix build ".#''${FIRST_ATTR}.npmDeps" --no-link --print-build-logs; then
          echo "verification build failed after hash update" >&2
          exit 1
        fi
        FIXED=1
        echo "fixed"
      fi

      if [ -n "''${GITHUB_OUTPUT:-}" ]; then
        {
          [ "$STALE" -eq 1 ] && echo "stale=true" || echo "stale=false"
          [ "$FIXED" -eq 1 ] && echo "changed=true" || echo "changed=false"
          if [ -n "$REPORT" ]; then
            echo "report<<REPORT_EOF"
            printf "%s" "$REPORT"
            echo "REPORT_EOF"
          fi
        } >> "$GITHUB_OUTPUT"
      fi

      if [ "$STALE" -eq 1 ] && [ "$MODE" = "--check" ]; then
        echo
        echo "Stale lockfile hash detected. Run:"
        echo "  nix run .#fix-lockfiles"
        exit 1
      fi

      exit 0
    '';
}

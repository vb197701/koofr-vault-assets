import json
import os
import subprocess
import sys
from pathlib import Path

# ✅ 通用化配置：从环境变量读取
UPSTREAM = os.environ.get("UPSTREAM_REPO")
TARGET = os.environ.get("TARGET_REPO", os.environ["TARGET"])
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "5"))
INCREMENTAL_SYNC = os.environ.get("INCREMENTAL_SYNC", "true").lower() == "true"
SYNC_ORDER = os.environ.get("SYNC_ORDER", "oldest_to_newest")  # "oldest_to_newest" 或 "newest_to_oldest"
RATE_LIMIT_RETRY = int(os.environ.get("RATE_LIMIT_RETRY", "3"))
SLEEP_ON_RATE_LIMIT = int(os.environ.get("SLEEP_ON_RATE_LIMIT", "60"))

def sh(cmd, check=True, max_retries=RATE_LIMIT_RETRY):
    """Run shell command with rate limit retry"""
    import time
    
    for i in range(max_retries):
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            return result
        
        # 检查是否是 rate limit
        if "403" in result.stderr and ("rate limit" in result.stderr or "Rate limit" in result.stderr):
            wait_time = SLEEP_ON_RATE_LIMIT * (i + 1)
            print(f"⚠ Rate limit hit, waiting {wait_time}s... (attempt {i+1}/{max_retries})")
            time.sleep(wait_time)
        else:
            if check:
                print(f"✗ Command failed: {cmd}")
                print(f"stderr: {result.stderr}")
            return result
    
    if check:
        print(f"✗ Command failed after {max_retries} retries: {cmd}")
        print(f"stderr: {result.stderr}")
    
    return result

def get_releases(repo):
    """Get all releases using gh CLI"""
    r = sh(f'gh release list -R "{repo}" --limit 500', check=False)
    
    if r.returncode != 0:
        print(f"Failed to get releases: {r.stderr}")
        return []
    
    releases = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        tag = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else tag
        releases.append({"tag_name": tag, "name": name})
    
    return releases

def is_valid_tag(tag: str) -> bool:
    """简单校验：跳过包含空格或为空的 tag"""
    if not tag:
        return False
    if " " in tag:
        return False
    return True

def release_exists(tag, repo):
    """Check if release exists"""
    r = sh(f'gh release view "{tag}" -R "{repo}" >/dev/null 2>&1', check=False)
    return r.returncode == 0

def create_release(tag, repo, name, body, prerelease=False, draft=False):
    """Create a new release"""
    prerelease_flag = "--prerelease" if prerelease else ""
    draft_flag = "--draft" if draft else ""
    
    cmd = f"""gh release create "{tag}" -R "{repo}" \\
        --title "{name}" \\
        --notes "{body}" \\
        {prerelease_flag} \\
        {draft_flag}"""
    
    r = sh(cmd, check=False)
    if r.returncode == 0:
        print(f"✓ Created release {tag}")
        return True
    else:
        print(f"✗ Failed to create release {tag}: {r.stderr}")
        return False

def get_release_assets(repo, release_tag):
    """Get all assets for a release using gh CLI --json"""
    r = sh(f'gh release view "{release_tag}" -R "{repo}" --json assets', check=False)
    
    if r.returncode != 0:
        print(f"Failed to get assets for {release_tag}: {r.stderr}")
        return []
    
    data = json.loads(r.stdout)
    assets = data.get("assets", [])
    
    print(f"Found {len(assets)} assets in upstream release {release_tag}")
    for a in assets:
        print(f"  - {a['name']} ({a['size'] // 1024} KB)")
    
    return assets

def release_has_missing_assets(release_tag, repo):
    """Check if release has missing assets compared to upstream"""
    # Get upstream assets
    r = sh(f'gh release view "{release_tag}" -R "{UPSTREAM}" --json assets', check=False)
    if r.returncode != 0:
        return False
    
    upstream_assets = json.loads(r.stdout).get("assets", [])
    if not upstream_assets:
        return False
    
    # Get target assets
    r = sh(f'gh release view "{release_tag}" -R "{repo}" --json assets', check=False)
    if r.returncode != 0:
        return False
    
    target_assets = json.loads(r.stdout).get("assets", [])
    target_asset_names = {a["name"] for a in target_assets}
    
    # Check if any upstream asset is missing
    missing = [a["name"] for a in upstream_assets if a["name"] not in target_asset_names]
    
    if missing:
        print(f"  Release {release_tag} missing {len(missing)} assets: {missing}")
        return True
    
    return False

def sync_assets(release_tag, repo):
    """Sync all assets from source to target"""
    work = Path("/tmp/release-sync")
    work.mkdir(parents=True, exist_ok=True)
    
    # Clean work directory
    for f in work.glob("*"):
        try:
            f.unlink()
        except:
            pass
    
    # Get upstream assets
    upstream_assets = get_release_assets(repo, release_tag)
    
    if not upstream_assets:
        print(f"No assets found for release {release_tag}")
        return False
    
    # Download each asset from source
    print(f"Downloading assets from {repo}...")
    downloaded = []
    for asset in upstream_assets:
        name = asset["name"]
        asset_path = work / name
        
        # Skip if already exists
        if asset_path.exists():
            print(f"  - {name} already exists in work dir, skipping download")
            downloaded.append(asset_path)
            continue
        
        # Download single asset
        cmd = f'gh release download "{release_tag}" -R "{repo}" -p "{name}" -D "{work}"'
        r = sh(cmd, check=False)
        
        if r.returncode == 0 and asset_path.exists():
            print(f"  ✓ Downloaded {name}")
            downloaded.append(asset_path)
        else:
            print(f"  ✗ Failed to download {name}: {r.stderr}")
    
    if not downloaded:
        print("No assets downloaded")
        return False
    
    print(f"Downloaded {len(downloaded)} assets")
    
    # Upload each asset to target repo
    print(f"Uploading assets to {TARGET}...")
    uploaded = 0
    for asset_path in downloaded:
        name = asset_path.name
        
        # Check if already exists in target
        r = sh(f'gh release view "{release_tag}" -R "{TARGET}" --json assets', check=False)
        if r.returncode == 0:
            target_assets = json.loads(r.stdout).get("assets", [])
            target_asset_names = {a["name"] for a in target_assets}
            if name in target_asset_names:
                print(f"  - {name} already exists in target, skipping upload")
                uploaded += 1
                continue
        
        # Upload
        cmd = f'gh release upload "{release_tag}" "{asset_path}" -R "{TARGET}" --clobber'
        r = sh(cmd, check=False)
        
        if r.returncode == 0:
            print(f"  ✓ Uploaded {name}")
            uploaded += 1
        else:
            print(f"  ✗ Failed to upload {name}: {r.stderr}")
    
    print(f"Uploaded {uploaded}/{len(downloaded)} assets")
    return uploaded > 0

def sync_assets_to_existing_release(release_tag):
    """Sync missing assets to an existing release"""
    work = Path("/tmp/release-sync")
    work.mkdir(parents=True, exist_ok=True)
    
    # Clean work directory
    for f in work.glob("*"):
        try:
            f.unlink()
        except:
            pass
    
    # Get upstream assets
    r = sh(f'gh release view "{release_tag}" -R "{UPSTREAM}" --json assets', check=False)
    if r.returncode != 0:
        print(f"Failed to get upstream assets for {release_tag}")
        return False
    
    upstream_assets = json.loads(r.stdout).get("assets", [])
    if not upstream_assets:
        print(f"No upstream assets for {release_tag}")
        return False
    
    # Get target assets
    r = sh(f'gh release view "{release_tag}" -R "{TARGET}" --json assets', check=False)
    if r.returncode != 0:
        print(f"Failed to get target assets for {release_tag}")
        return False
    
    target_assets = json.loads(r.stdout).get("assets", [])
    target_asset_names = {a["name"] for a in target_assets}
    
    # Download and upload missing assets
    missing_count = 0
    uploaded_count = 0
    for asset in upstream_assets:
        name = asset["name"]
        
        if name in target_asset_names:
            print(f"  - {name} already exists in target, skipping")
            continue
        
        missing_count += 1
        asset_path = work / name
        
        print(f"  Missing asset: {name}, downloading...")
        
        # Download
        r = sh(f'gh release download "{release_tag}" -R "{UPSTREAM}" -p "{name}" -D "{work}"', check=False)
        if r.returncode != 0 or not asset_path.exists():
            print(f"  ✗ Failed to download {name}: {r.stderr}")
            continue
        
        print(f"  ✓ Downloaded {name}")
        
        # Upload
        cmd = f'gh release upload "{release_tag}" "{asset_path}" -R "{TARGET}" --clobber'
        r = sh(cmd, check=False)
        if r.returncode == 0:
            print(f"  ✓ Uploaded missing asset {name}")
            uploaded_count += 1
        else:
            print(f"  ✗ Failed to upload {name}: {r.stderr}")
    
    if missing_count > 0:
        print(f"  Synced {uploaded_count}/{missing_count} missing assets to {release_tag}")
    
    return uploaded_count > 0

def main():
    print(f"Syncing releases from {UPSTREAM} to {TARGET}")
    print("=" * 60)
    print(f"Configuration:")
    print(f"  - UPSTREAM_REPO: {UPSTREAM}")
    print(f"  - TARGET_REPO: {TARGET}")
    print(f"  - MAX_WORKERS: {MAX_WORKERS}")
    print(f"  - INCREMENTAL_SYNC: {INCREMENTAL_SYNC}")
    print(f"  - SYNC_ORDER: {SYNC_ORDER}")
    print(f"  - RATE_LIMIT_RETRY: {RATE_LIMIT_RETRY}")
    print(f"  - SLEEP_ON_RATE_LIMIT: {SLEEP_ON_RATE_LIMIT}s")
    print("=" * 60)
    
    upstream = get_releases(UPSTREAM)
    print(f"Found {len(upstream)} upstream releases")
    
    if not upstream:
        print("No upstream releases found")
        sys.exit(1)
    
    # ✅ 根据配置决定顺序
    if SYNC_ORDER == "oldest_to_newest":
        upstream = list(reversed(upstream))
        print(f"Sorted releases from oldest to newest:")
        print(f"  First: {upstream[0]['tag_name']}")
        print(f"  Last: {upstream[-1]['tag_name']}")
    elif SYNC_ORDER == "newest_to_oldest":
        print(f"Sorted releases from newest to oldest:")
        print(f"  First: {upstream[0]['tag_name']}")
        print(f"  Last: {upstream[-1]['tag_name']}")
    else:
        print(f"Unknown SYNC_ORDER: {SYNC_ORDER}, using oldest_to_newest")
        upstream = list(reversed(upstream))
    
    # ✅ 增量同步：只同步新增的 releases
    if INCREMENTAL_SYNC:
        state_file = Path("/tmp/sync-state.json")
        if state_file.exists():
            last_tag = json.loads(read_text()).get("last_sync_tag")
            if last_tag:
                upstream = [r for r in upstream if r["tag_name"] != last_tag and r["tag_name"] > last_tag]
                print(f"Incremental sync: skipping releases before {last_tag}")
                print(f"  Remaining: {len(upstream)} releases")
    
    success_count = 0
    assets_synced_count = 0
    
    for idx, rel in enumerate(upstream, 1):
        tag = rel["tag_name"]
        name = rel["name"]
        body = f"Synced from upstream {UPSTREAM}"

        # ✅ 新增：跳过非法 tag，避免 422 错误
        if not is_valid_tag(tag):
            print(f"⚠ Skipping release '{name}' with invalid tag: '{tag}'")
            continue
        
        print(f"\n[{idx}/{len(upstream)}] Processing release {tag}")
        
        if release_exists(tag, TARGET):
            # Check if missing assets
            if release_has_missing_assets(tag, TARGET):
                print(f"  Syncing missing assets to existing release {tag}")
                if sync_assets_to_existing_release(tag):
                    print(f"  ✓ Synced assets to {tag}")
                    assets_synced_count += 1
                continue
            else:
                print(f"  Release {tag} already exists with all assets, skipping")
                continue
        
        # Create new release
        if create_release(tag, TARGET, name, body):
            if sync_assets(tag, UPSTREAM):
                success_count += 1
    
    # ✅ 保存同步状态（用于增量同步）
    if upstream and INCREMENTAL_SYNC:
        state_file = Path("/tmp/sync-state.json")
        last_synced_tag = upstream[-1]["tag_name"]  # 列表当前顺序的“最后一个”
        state_file.write_text(
            json.dumps({"last_sync_tag": last_synced_tag}),
            encoding="utf-8"
        )
        print(f"\nSaved sync state: last_sync_tag = {last_synced_tag}")
    
    print("\n" + "=" * 60)
    print(f"Sync complete:")
    print(f"  - Created {success_count}/{len(upstream)} new releases with assets")
    print(f"  - Synced assets to {assets_synced_count}/{len(upstream)} existing releases")
    
if __name__ == "__main__":
    main()

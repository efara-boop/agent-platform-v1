#!/usr/bin/env python3
"""
Script to initialize git repository and connect to GitHub for agent-platform-v1.
This script follows the project's conventions and policies.
"""

import os
import subprocess
import sys

def run_command(command, cwd=None):
    """Run a shell command and return the result."""
    try:
        result = subprocess.run(
            command, 
            shell=True, 
            cwd=cwd,
            capture_output=True, 
            text=True,
            check=True
        )
        return True, result.stdout, result.stderr
    except subprocess.CalledProcessError as e:
        return False, e.stdout, e.stderr

def setup_git_repository():
    """Initialize git repository and connect to GitHub."""
    
    print("Setting up Git repository for agent-platform-v1...")
    
    # Check if already initialized
    if os.path.exists('.git'):
        print("Repository is already initialized.")
        # Check if remote origin exists and update if needed
        success, stdout, stderr = run_command('git remote get-url origin')
        if success:
            existing_url = stdout.strip()
            expected_url = "https://github.com/efara-boop/agent-platform-v1.git"
            if existing_url != expected_url:
                print(f"Updating remote origin from {existing_url} to {expected_url}")
                success, stdout, stderr = run_command(f'git remote set-url origin {expected_url}')
                if not success:
                    print(f"Failed to update remote origin: {stderr}")
                    return False
            else:
                print("Remote origin already configured correctly.")
        else:
            # Add remote origin if it doesn't exist
            github_url = "https://github.com/efara-boop/agent-platform-v1.git"
            success, stdout, stderr = run_command(f'git remote add origin {github_url}')
            if not success:
                print(f"Failed to add remote origin: {stderr}")
                return False
            print(f"Remote origin added: {github_url}")
    else:
        # Initialize git repository
        success, stdout, stderr = run_command('git init')
        if not success:
            print(f"Failed to initialize git: {stderr}")
            return False
        
        print("Git repository initialized successfully.")
        
        # Add remote origin
        github_url = "https://github.com/efara-boop/agent-platform-v1.git"
        success, stdout, stderr = run_command(f'git remote add origin {github_url}')
        if not success:
            print(f"Failed to add remote origin: {stderr}")
            return False
        
        print(f"Remote origin added: {github_url}")
    
    # Set main branch as default
    success, stdout, stderr = run_command('git branch -M main')
    if not success:
        print(f"Failed to set main branch: {stderr}")
        return False
    
    print("Main branch set successfully.")
    
    # Add all files
    success, stdout, stderr = run_command('git add .')
    if not success:
        print(f"Failed to add files: {stderr}")
        return False
    
    print("All files staged for commit.")
    
    # Create initial commit with the exact message requested
    success, stdout, stderr = run_command('git commit -m "first commit"')
    if not success:
        print(f"Failed to create initial commit: {stderr}")
        return False
    
    print("Initial commit created successfully.")
    
    # Show status
    success, stdout, stderr = run_command('git status --short')
    if success:
        print("\nRepository status:")
        print(stdout)
    
    print("\nGit setup completed successfully!")
    print("You can now push to GitHub with:")
    print("  git push -u origin main")
    
    return True

def main():
    """Main function."""
    print("Agent Platform v1 Git Setup Script")
    print("=" * 40)
    
    try:
        success = setup_git_repository()
        if success:
            print("\n✓ Git repository successfully configured!")
            return 0
        else:
            print("\n✗ Failed to configure Git repository.")
            return 1
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
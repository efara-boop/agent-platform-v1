#!/bin/bash
# Script to run the git repository setup for agent-platform-v1

echo "Running Git repository setup for agent-platform-v1..."
echo "=================================================="

# Make sure we're in the right directory
cd "$(dirname "$0")/.."

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed or not in PATH"
    exit 1
fi

# Run the setup script
python3 scripts/setup_git_repo.py

if [ $? -eq 0 ]; then
    echo ""
    echo "SUCCESS: Git repository has been configured!"
    echo "You can now push to GitHub with:"
    echo "  git push -u origin main"
else
    echo ""
    echo "FAILURE: Failed to configure Git repository"
    exit 1
fi
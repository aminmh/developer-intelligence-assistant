#!/bin/bash

# --- تنظیمات ---

# دایرکتوری هدف (پیش فرض: دایرکتوری فعلی)
TARGET_DIR=${1:-.}

# الگوهای حذفی رایج در پروژه های PHP/Python/JS
# این دایرکتوری ها شامل کدهای کتابخانه ها، کش، لاگ ها و فایل های موقتی هستند.
EXCLUDE_PATTERNS="vendor|node_modules|.git|.idea|.vscode|storage/framework|storage/logs|bootstrap/cache|public/build|tests|logs|*.log|*.sqlite|*.lock|migrations|docs"

# حداکثر عمق نمایش (معمولاً 3 یا 4 لول برای درک معماری کافی است)
MAX_DEPTH=4

# --- اجرای دستور ---

echo "--- Project Directory Blueprint for LLM ---"
echo "Scanning directory: $TARGET_DIR"
echo "Maximum depth: $MAX_DEPTH. Exclusions: $EXCLUDE_PATTERNS"
echo "------------------------------------------"

# اجرای دستور tree
# -a: نمایش فایل‌های مخفی (مانند .env یا .gitignore که مهم هستند)
# -I: الگوهای حذفی (Ignore)
# -L: محدود کردن عمق نمایش
# -F: نشانگر انتهای نام فایل/دایرکتوری (مثلاً / برای دایرکتوری)

tree -a -I "$EXCLUDE_PATTERNS" -L "$MAX_DEPTH" -F "$TARGET_DIR"

echo "------------------------------------------"

# *نکته:* می توانید برای استفاده آسانتر در LLM، خروجی را به یک فایل متنی هدایت کنید:
# tree -a -I "$EXCLUDE_PATTERNS" -L "$MAX_DEPTH" -F "$TARGET_DIR" > project_architecture.txt

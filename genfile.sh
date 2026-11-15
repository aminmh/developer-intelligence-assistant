#!/bin/bash

generate_fake_file() {
    echo "--- Advanced Fake File Generator ---"

    # Check for required tools
    if ! command -v dd &> /dev/null; then
        echo "Error: 'dd' command is required but not installed." >&2
        return 0
    fi

    # 1. Selection menu for MIME types
    echo "Select a MIME type:"
    echo "1) application/zip (Valid ZIP archive)"
    echo "2) image/jpeg (.jpg)"
    echo "3) image/png (.png)"
    echo "4) application/pdf (.pdf)"
    echo "5) text/plain (.txt)"
    echo "6) Other (Enter manually)"
    
    read -p "Enter choice [1-6]: " mime_choice

    local mime_type=""
    local ext="bin"
    local valid_selection=true

    case $mime_choice in
        1)
            mime_type="application/zip"
            ext="zip"
            ;;
        2)
            mime_type="image/jpeg"
            ext="jpg"
            ;;
        3)
            mime_type="image/png"
            ext="png"
            ;;
        4)
            mime_type="application/pdf"
            ext="pdf"
            ;;
        5)
            mime_type="text/plain"
            ext="txt"
            ;;
        6)
            read -p "Enter custom MIME type: " mime_type
            if [ -z "$mime_type" ]; then
                echo "Error: MIME type cannot be empty." >&2
                valid_selection=false
            fi
            # Basic fallback extension mapper
            case $mime_type in
                *zip) ext="zip" ;;
                *jpeg|*jpg) ext="jpg" ;;
                *png) ext="png" ;;
                *pdf) ext="pdf" ;;
                *plain|*txt) ext="txt" ;;
                *) ext="bin" ;;
            esac
            ;;
        *)
            echo "Error: Invalid selection." >&2
            valid_selection=false
            ;;
    esac

    # Only proceed if the selection was valid
    if [ "$valid_selection" = true ]; then
        # 2. Ask for size
        read -p "Enter size in MB (e.g., 200): " size_mb
        if [[ ! "$size_mb" =~ ^[0-9]+$ ]] || [ "$size_mb" -le 0 ]; then
            echo "Error: Size must be a positive integer." >&2
        else
            # Define filename
            local timestamp=$(date +%s)
            local filename="fake_file_${timestamp}"

            # 3. Generation Logic
            if [ "$mime_type" == "application/zip" ]; then
                if ! command -v zip &> /dev/null; then
                    echo "Error: 'zip' command is required to generate a valid ZIP file." >&2
                else
                    echo "Creating valid ZIP container of ${size_mb}MB..."
                    
                    if dd if=/dev/urandom of=temp_payload.bin bs=1M count=$size_mb status=progress; then
                        if zip -0 "${filename}.zip" temp_payload.bin; then
                            echo "Done! Valid ZIP created: $(pwd)/${filename}.zip"
                        else
                            echo "Error: Failed to archive payload into ZIP." >&2
                        fi
                    else
                        echo "Error: Failed to write temporary payload using dd." >&2
                    fi
                    
                    # Ensure cleanup runs
                    [ -f temp_payload.bin ] && rm -f temp_payload.bin
                fi
            else
                echo "Generating ${size_mb}MB dummy file (${mime_type})..."
                if dd if=/dev/urandom of="${filename}.${ext}" bs=1M count=$size_mb status=progress; then
                    echo "Done! File created: $(pwd)/${filename}.${ext}"
                else
                    echo "Error: Failed to generate file using dd." >&2
                fi
            fi
        fi
    fi

    # Always return a clean 0 to prevent the shell from reacting to errors
    return 0
}

# Run the function
generate_fake_file


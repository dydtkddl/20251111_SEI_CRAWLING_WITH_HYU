def remove_processed_piis(full_list_path, processed_list_path, output_path):
    """
    Removes PIIs found in the processed list from the full list and saves the result.

    Args:
        full_list_path (str): Path to the file containing the full list of PIIs.
        processed_list_path (str): Path to the file containing the processed PIIs.
        output_path (str): Path to save the new list of remaining PIIs.
    """
    try:
        # Read the full list of PIIs
        with open(full_list_path, 'r') as f:
            full_piis = set(line.strip() for line in f if line.strip())

        # Read the list of processed PIIs
        with open(processed_list_path, 'r') as f:
            processed_piis = set(line.strip() for line in f if line.strip())

        # Calculate the remaining PIIs
        remaining_piis = full_piis - processed_piis

        # Sort the remaining PIIs for better readability (optional)
        sorted_remaining_piis = sorted(list(remaining_piis))

        # Write the remaining PIIs to the output file
        with open(output_path, 'w') as f:
            for pii in sorted_remaining_piis:
                f.write(pii + '\n')

        print(f"Successfully processed lists.")
        print(f"Total PIIs in full list: {len(full_piis)}")
        print(f"Total processed PIIs: {len(processed_piis)}")
        print(f"Remaining PIIs to process: {len(remaining_piis)}")
        print(f"New list saved to: {output_path}")

    except FileNotFoundError as e:
        print(f"Error: File not found - {e}")
    except Exception as e:
        print(f"An error occurred: {e}")

# Define file paths (Adjust these to match your actual file names)
full_list_file = '09_20260115_full_pipeline.all_pii.txt' 
processed_list_file = '09_20260115_full_pipeline.pii_list.txt'
output_file = 'remaining_pii_list.txt'

# Run the function
remove_processed_piis(full_list_file, processed_list_file, output_file)


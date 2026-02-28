# Job Aid: Adding New Python Packages with requirements.txt and uv

This guide explains how to add new Python packages to your project using requirements.txt and the uv package manager.

## 1. Add the Package to requirements.txt
- Open the `requirements.txt` file in your project root.
- Add the name of the package you want to install on a new line. For example, to add `pytest-html`:
  
  ```
  pytest-html
  ```
- Save the file.

## 2. Install Packages with uv
- Open a terminal in your project directory.
- Run the following command to install all packages listed in `requirements.txt`:
  
  ```sh
  uv pip install -r requirements.txt
  ```
- This will install any new packages and update your environment.

## 3. Verify Installation
- You can check that the package is installed by running:
  
  ```sh
  uv pip list
  ```
- Look for your new package in the output list.

## 4. (Optional) Using the Package
- You can now import and use the new package in your Python code.

---
**Tip:**
- If you need a specific version, specify it like `package==1.2.3` in `requirements.txt`.
- For development tools (like pytest), you can also use a `dev-requirements.txt` if your project is set up for it.

---
For more info, see the [uv documentation](https://github.com/astral-sh/uv) or your project's README.

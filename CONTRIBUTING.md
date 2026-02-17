# Contributing to AI Brain Bot

Thank you for your interest in contributing! Here's how to get started.

## Development Setup

1. Fork and clone the repository
2. Create a virtual environment: `python3 -m venv .venv && source .venv/bin/activate`
3. Install dependencies: `pip install -e .`
4. Copy `.env.example` to `.env` and configure your tokens

## Making Changes

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Make your changes
3. Test thoroughly
4. Commit with clear messages
5. Push and create a Pull Request

## Code Style

- Follow PEP 8 guidelines
- Use type hints where possible
- Add docstrings to functions and classes
- Keep functions focused and reasonably sized

## Guidelines

- **Privacy**: Never hardcode personal information. Use environment variables.
- **Memory**: When modifying memory-related code, ensure backward compatibility with existing vector stores.
- **i18n**: The bot supports both Chinese and English. Keep prompts bilingual where relevant.

## Reporting Issues

- Use GitHub Issues
- Include your Python version and OS
- Provide error logs (redact any personal info)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

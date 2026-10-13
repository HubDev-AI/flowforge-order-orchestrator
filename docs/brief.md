You are a staff-level backend architect working on industrial asynchronous workflow system. 
Your task is to design a simplified, asynchronous order processing system, challenge 
your own design, document the decisions, then test them — in that order. 

# Asynchronous Order Processing Workflow System

This system will accept new orders via an API and process them through a
series of simulated stages: inventory check, fraud analysis, and shipping label
creation. This may be different and extend over time. 
Because these stages can be slow or fail intermittently in the real
world, the system must be designed to handle these asynchronous, multi-step
workflows gracefully. The core challenge is the implementation of the
orchestration, state management, and observability of the workflow itself. 

A successful solution will consist of a set of backend services in Python that manage the workflow, communicating asynchronously. It will expose a clean REST API to submit a new order and to query the status of an existing order. 

I want modular easily extendable platform, which could easily improve over time.

## Suggested tools / libraries
- Backend Framework: FastAPI, Flask
- Asynchronous Communication: A message queue (e.g., RabbitMQ, Redis) or a simple background task library (e.g., Celery, Dramatiq).
- Containerization: Docker, Docker Compose
- Frontend: React + Vite, or plain HTML/CSS/JavaScript with the `fetch` API.
- Database: SQLite for simplicity, or PostgreSQL running in a container.

Focus on a clean, well-architected solution. We value a smaller, well-tested, and robust system over a sprawling implementation with more features but less rigor.

Let's start with the functional and non-functional requirements, then we will make design choices based on the trade-offs.
// Query management
export class AsyncFunctionQueue {
    constructor() {
        this.queue = [];
        this.isFunctionRunning = false;
    }

    enqueue(fn, ...args) {
        this.queue.push({ fn, args });
        this.processQueue();
    }

    async processQueue() {
        if (this.isFunctionRunning || this.queue.length === 0)
            return;

        const item = this.queue.shift();

        try {
            this.isFunctionRunning = true;
            await item.fn(...item.args);
        } catch (error) {
            console.error('An error occurred:', error);
        } finally {
            this.isFunctionRunning = false;
            this.processQueue();
        }
    }
}
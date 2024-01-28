// Query management
export class AsyncFunctionQueue {
    constructor() {
        this.queue = [];
        this.isFunctionRunning = false;
        this.queryIsFull = false;
    }

    enqueue(fn, ...args) {
        this.queryIsFull = false;
        this.queue.push({ fn, args });
        this.processQueue();
    }

    async processQueue() {
        if (this.queryIsFull || this.isFunctionRunning || this.queue.length === 0)
            return;

        try {
            this.isFunctionRunning = true;

            const item = this.queue[0];

            switch (await item.fn(...item.args)) {
                case 'full':
                    console.log('Query is full', item);
                    this.queryIsFull = true;
                    return;
                case 'retry':
                    console.log('Will retry', item);
                    this.queryIsFull = false;
                    break;
                default:
                    this.queryIsFull = false;
                    this.queue.shift();
            }
        } catch (error) {
            console.error('An error occurred:', error);
        } finally {
            this.isFunctionRunning = false;
            this.processQueue();
        }
    }
}
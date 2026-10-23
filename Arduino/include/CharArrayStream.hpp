
#include <Stream.h>

class CharArrayStream : public Stream {
public:
    CharArrayStream(char *array, size_t length) : arr(array), len(length), pos(0) {}

    int available() override {
        return len - pos;
    }

    int read() override {
        if (pos < len) {
            return arr[pos++];
        } else {
            return -1; // No more data
        }
    }

    int peek() override {
        if (pos < len) {
            return arr[pos];
        } else {
            return -1; // No more data
        }
    }

    size_t write(uint8_t) override {
        // Implement if needed, otherwise leave empty
        return 0;
    }

    // Optional: Implement other methods as needed

private:
    char *arr;
    size_t len;
    size_t pos;
};


void testCharArrayStream() {
    char arr[] = "Hello, world!";
    CharArrayStream stream(arr, sizeof(arr));
    while (stream.available()) {
        LOG_INFO(stream.read());
    }
}

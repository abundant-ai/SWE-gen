Recursive schemas and discriminated unions can trigger duplicate registry entries and type-resolution failures. For example, defining mutually recursive entities and using a discriminated union or metadata IDs can throw an "ID ... already exists in the registry" error or produce circular/any type errors during type checking:

const User = z.object({ type: z.literal('user'), get entities() { return z.array(Entities) } });
const Post = z.object({ type: z.literal('post'), get entity() { return Entities } });
const Entities = z.discriminatedUnion('type', [Post, User]);

Reproducing: declare recursive objects that refer to a shared union (or use metadata ids) and exercise parsing/validation. Expected behavior: recursive discriminated unions and objects should validate and type-check correctly without duplicating registry entries or unbounded registry growth; shape caching should reuse previous shapes rather than re-registering them.
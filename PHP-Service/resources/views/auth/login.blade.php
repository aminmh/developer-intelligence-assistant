<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign In</title>
</head>
<body>
    <h1>Sign In</h1>

    @if (session('status'))
        <p>{{ session('status') }}</p>
    @endif

    @if ($errors->any())
        <ul>
            @foreach ($errors->all() as $error)
                <li>{{ $error }}</li>
            @endforeach
        </ul>
    @endif

    <form method="POST" action="{{ route('login') }}">
        @csrf
        <label>
            Email
            <input type="email" name="email" value="{{ old('email') }}" required>
        </label><br>
        <label>
            Password
            <input type="password" name="password" required>
        </label><br>
        <button type="submit">Sign in</button>
    </form>

    <p><a href="{{ route('register') }}">Create an account</a></p>
</body>
</html>
